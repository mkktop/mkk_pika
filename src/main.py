import os
import threading
from datetime import datetime
import urllib3
from time import sleep
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from logger import  logger
from api import *
from database import ComicSQLiteDB

_SIZE_LOCK = threading.Lock()
_downloaded_bytes = 0
_download_limits = None
# 本次运行实际下载了内容的漫画编号
_downloaded_seqs = set()

def add_downloaded_bytes(num_bytes: int):
    """累加本次运行已下载的字节数"""
    global _downloaded_bytes
    with _SIZE_LOCK:
        _downloaded_bytes += num_bytes

def downloaded_bytes() -> int:
    with _SIZE_LOCK:
        return _downloaded_bytes

def _read_gb(key: str, default: float) -> int:
    try:
        return int(float(get_config("download", key, default) or 0) * 1024 ** 3)
    except (TypeError, ValueError):
        return int(default * 1024 ** 3)

def download_limits():
    """(软上限, 硬上限) 字节数，只读一次配置。
    软上限：一本漫画完整下载完后，累计超过它就不再开始下一本（0 表示不限制）
    硬上限：单本漫画下载途中达到它就立即停止，保证不会撑爆磁盘"""
    global _download_limits
    if _download_limits is None:
        soft = _read_gb("max_size_gb", 6)
        hard = _read_gb("hard_max_size_gb", 7)
        if soft <= 0:
            hard = 0
        elif hard <= 0 or hard < soft:
            hard = soft
        _download_limits = (soft, hard)
    return _download_limits

def size_limit_reached() -> bool:
    """已达到软上限：当前漫画下完后不再开始下一本"""
    soft, _ = download_limits()
    return soft > 0 and downloaded_bytes() >= soft

def hard_limit_reached() -> bool:
    """已达到硬上限：立即停止当前漫画的后续图片下载"""
    _, hard = download_limits()
    return hard > 0 and downloaded_bytes() >= hard

def format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.2f} GB"
    return f"{num_bytes / 1024 ** 2:.2f} MB"

def download(name_len,folder_path: str, i: int, url: str, retries=3,):
    for attempt in range(retries):
        path = os.path.join(folder_path, (str(i + 1).zfill(name_len)+'.jpg'))
        try:
            if os.path.exists(path):
                return
            response = http_do("GET", url=url)
            if response.status_code == 200:
                with open(path, 'wb') as f:
                    f.write(response.content)
                add_downloaded_bytes(len(response.content))
                return
            else:
                logger.warning(f"Attempt {attempt + 1} failed for {url}, status code: {response.status_code}")
        except requests.exceptions.Timeout:
            logger.error(f"Attempt {attempt+1} timeout for {url}")
        except Exception as e:
            logger.error(f"Attempt {attempt+1} error for {url}: {e}")
    raise Exception(f"Failed to download {url} after {retries} attempts.")

def episode_key(episode) -> str:
    """章节唯一标识：用 API 的 order，同名章节也不会互相覆盖"""
    return str(episode["order"])

#下载漫画
def download_comic(comic,executor:ThreadPoolExecutor):
    """下载一本漫画，返回 ok / failed / limit / stuck / skip"""
    if size_limit_reached():
        soft, _ = download_limits()
        logger.info(f"已达到本次下载上限({format_size(soft)})，跳过{comic.get('title')}")
        return "skip"
    cid = comic["_id"]
    title = comic["title"]
    logger.info(f"开始检查{title}是否存在未下载章节")
    author = comic["author"]
    categories = comic["categories"]
    episodes = episodes_all(cid, title)
    if episodes is None:
        logger.error(f"获取{title}的章节失败，本轮跳过，下次重试")
        return "failed"
    #章节信息入库，网站失效后也能还原章节名
    db.save_chapters(cid, episodes)
    num_pages = comic["pagesCount"] if "pagesCount" in comic else -1
    is_detail = get_config("download","is_detail")
    if db.is_comic_downloaded(comic["_id"]):
        episodes = [episode for episode in episodes
                    if not db.is_episode_downloaded(comic["_id"], episode_key(episode))]
    if episodes:
        logger.info(
            '正在下载:[%s]-[%s]-[%s]-[total_pages:%d]' %
            (title, author, categories, num_pages)
        )
        logger.info(episodes)
    else:
        logger.info(f"{title}中没有可下载章节")
        return "ok"
    #数据库加入该漫画，返回编号（从1开始自增）
    seq = db.mark_comic_as_downloaded(comic["_id"])
    bytes_before = downloaded_bytes()
    comic_path = os.path.join(".",
                              "comics",
                              f"{convert_file_name(title)}"
                            )
    comic_path = ensure_valid_path(comic_path)
    completed_episodes = 0
    failed_episodes = 0
    for episode in episodes:
        if hard_limit_reached():
            _, hard = download_limits()
            logger.info(f"已达到单本漫画硬上限({format_size(hard)})，剩余章节留待下次")
            break
        chapter_title = f"{episode_key(episode)}_{convert_file_name(episode['title'])}"
        chapter_path = os.path.join(comic_path, chapter_title)
        chapter_path = Path(chapter_path)
        chapter_path.mkdir(parents=True, exist_ok=True)
        image_urls = []
        current_page = 1
        fetch_failed = False
        while True:
            try:
                page_data = json.loads(
                    picture(cid, episode["order"], current_page).content
                )["data"]["pages"]["docs"]
            except Exception as e:
                logger.error(f"{title}{chapter_title}获取图片列表失败(page:{current_page})：{e}")
                fetch_failed = True
                break
            # print_full_json(page_data)
            current_page += 1
            if page_data:
                image_urls.extend(list(map(
                    lambda i: i['media']['fileServer'] + '/static/' + i['media']['path'],
                    page_data
                )))
            else:
                break
        if fetch_failed:
            failed_episodes += 1
            continue
        if not image_urls:
            logger.warning(f"{title}{chapter_title}没有找到图片")
            failed_episodes += 1
            continue
        logger.info(f"找到 {len(image_urls)} 张图片在:{chapter_title}")
        if len(image_urls) <1000:
            name_len = 3
        else:
            name_len = 4

        downloaded_count = 0
        futures = {}
        for index, image_url in enumerate(image_urls):
            if hard_limit_reached():
                break
            futures[executor.submit(download,name_len,
                            chapter_path,
                            index,image_url,)] = index
        limit_hit = len(futures) < len(image_urls)
        for future in as_completed(futures):
            index = futures[future]
            try:
                future.result()
                downloaded_count += 1
            except Exception as e:
                logger.error(f"Error downloading the {index+1}-th image"
                          f"in episode:{episode['title']}"
                          f"in comic:{title}"
                          f"Exception:{e}")
                continue
        if is_detail:
            episode_title = episode["title"]
            logger.info(
                f"[episode:{episode_title:<10}] "
                f"downloaded:{downloaded_count:>6}, "
                f"total:{len(image_urls):>4}, "
                f"progress:{int(downloaded_count / len(image_urls) * 100):>3}%",
            )
        if downloaded_count == len(image_urls):
            db.update_downloaded_episodes(comic["_id"], episode_key(episode))
            completed_episodes += 1
        elif limit_hit:
            logger.info(
                f"达到单本漫画硬上限，{chapter_title}已下载"
                f"{downloaded_count}/{len(image_urls)}张，剩余留待下次"
            )
        else:
            episode_title = episode["title"]
            failed_episodes += 1
            logger.error(
                f"Failed to download the episodes:{episode_title} "
                f"of comic:{title}. "
                f"Currently, {downloaded_count} images(total_images:{len(image_urls)}) "
                "from this episode have been downloaded"
            )
        sleep(1)
    if downloaded_bytes() > bytes_before:
        _downloaded_seqs.add(seq)
    if hard_limit_reached():
        return "limit" if completed_episodes > 0 else "stuck"
    if failed_episodes:
        logger.error(f"{title}有{failed_episodes}个章节没下完，下次重试")
        return "failed"
    return "ok"


def download_all_comics(page: int):
    """扫描全站指定页，返回 (页码, 总页数, 总漫画数, 待下载漫画列表)。
    游标由调用方在整页下载完成后推进，避免中途中断导致漏掉未下载内容。"""
    all_comics = []
    categories = get_config("download", "filter").split(',')
    db.mark_download_info_id("old_to_nwe")
    res = get_old_update(1)
    total_page =  res["pages"]
    total_comic = res["total"]
    if page > total_page:
        return page, total_page, total_comic, []
    logger.info(f"现在开始扫描第{page}/{total_page}页")
    page_docs = get_old_update(page)["docs"]
    for doc in page_docs:
        intersection = set(doc['categories']).intersection(set(categories))
        if len(intersection) == 0:
            all_comics.append(doc)
    return page, total_page, total_comic, all_comics

def record_comic_info(comic_id: str):
    """获取漫画详情、打印信息并写入数据库，返回漫画详情"""
    info = comic_info(comic_id)
    data = info["data"]['comic']
    comic_id = data["_id"]
    title = data["title"]
    author = data["author"]
    finished = data["finished"]
    pagesCount = data["pagesCount"]
    category_list = data["categories"]
    category = ",".join(category_list) if isinstance(category_list, list) else ""
    epsCount = data["epsCount"]
    update_time = data["updated_at"]
    description = data.get("description") or ""
    tags = data.get("tags") or []
    tags = ",".join(map(str, tags)) if isinstance(tags, list) else ""
    logger.info(
        f"""

                ==================== 漫画信息 ====================
                漫画ID：{comic_id}
                标题：{title}
                作者：{author}
                是否完结：{"是" if finished else "否"}
                总页数：{pagesCount}
                分类：{category}
                章节数：{epsCount}
                最后更新时间：{update_time}
                =================================================
                    """.strip()  # strip() 去掉首尾空行，让日志更整洁
    )
    db.save_comic({
        "comic_id": comic_id,
        "title": title,
        "author": author,
        "finished": finished,
        "pagesCount": pagesCount,
        "category": category,
        "epsCount": epsCount,
        "update_time": update_time,
        "description": description,
        "tags": tags,
    })
    return data

if __name__ == "__main__":
    logger.info("=======================================================================")
    #初始化数据库
    db = ComicSQLiteDB("./data/comic_spider.db")
    #获取累计的下载数量
    logger.info('已经累计下载%d本漫画' %db.get_downloaded_comic_count())
    soft, hard = download_limits()
    if soft:
        logger.info('本次运行下载上限：%s（单本漫画硬上限：%s）' % (format_size(soft), format_size(hard)))
    else:
        logger.info('本次运行下载上限：不限制')
    #登录
    login()
    #获取下载的线程数
    thread_number = int(get_config(section="download", key="thread_number"))
    logger.info("开始分批下载全部章节")
    db.create_download_all_info()
    download_plan = int(get_config(section="download", key="download_plan", default_value=0))
    max_retry = int(get_config(section="download", key="max_retry", default_value=3))
    if size_limit_reached():
        logger.info("已达到本次下载上限，跳过分批下载")
    elif download_plan > 0:
        # download_plan 是本次最多扫描的分页数，真正的停止条件是下载大小上限
        cursor = db.get_download_info_pagesCount("old_to_nwe") or 0
        scan_page = cursor
        for download_num in range(download_plan):
            if size_limit_reached():
                logger.info("已达到本次下载上限，停止本次分批下载")
                break
            scan_page += 1
            page, total_page, total_comic, the_all_comics = download_all_comics(scan_page)
            if page > total_page:
                logger.info("全部分页已扫描完毕")
                break
            logger.info('第%d/%d页共%d本漫画待下载' % (page, total_page, len(the_all_comics)))
            page_complete = True
            with ThreadPoolExecutor(max_workers=thread_number) as executor:
                for the_comic in the_all_comics:
                    # 一本漫画完整下完后累计超过软上限，就不再开始下一本
                    if size_limit_reached():
                        logger.info("已达到本次下载上限，本页剩余漫画留待下次")
                        page_complete = False
                        break
                    cid = the_comic["_id"]
                    attempts = db.get_comic_failure_count(cid)
                    if attempts >= max_retry:
                        logger.error(
                            f"{the_comic.get('title')}已连续失败{attempts}次，"
                            "跳过并保留在 comic_retry 表，避免卡住全站游标"
                        )
                        continue
                    try:
                        # 开始下载
                        result = download_comic(the_comic, executor)
                    except Exception as e:
                        times = db.add_comic_failure(cid, e)
                        page_complete = False
                        logger.error(
                            'Download failed for {}, with Exception:{} ({}/{})'.format(
                                the_comic["title"], e, times, max_retry)
                        )
                        continue
                    try:
                        record_comic_info(cid)
                    except Exception as e:
                        # 元数据保存失败不影响已下载内容，也不该卡住游标
                        logger.error(f"{the_comic.get('title')}元数据保存失败：{e}")
                    if result == "ok":
                        db.clear_comic_failure(cid)
                    elif result in ("failed", "stuck"):
                        times = db.add_comic_failure(cid)
                        logger.error(
                            f"{the_comic.get('title')}下载未完成({result})，"
                            f"累计失败{times}/{max_retry}次，游标不推进"
                        )
                        page_complete = False
                    elif result == "limit":
                        page_complete = False
            if hard_limit_reached():
                page_complete = False
            if not page_complete:
                # 本页没下完：游标停在原处，但继续往下扫，避免整轮运行被一页卡住
                logger.info('第%d页未下载完，游标保持第%d页，下次从第%d页继续'
                            % (page, cursor, cursor + 1))
                continue
            if page != cursor + 1:
                logger.info('第%d页完成，但第%d页仍未完成，游标保持第%d页'
                            % (page, cursor + 1, cursor))
                continue
            cursor = page
            db.save_download_info({
                "comic_id": "old_to_nwe",
                "pagesCount": cursor,
                "total_comic": total_comic,
                "totalPages": total_page,
            })
            logger.info('第%d/%d页完成，本次已下载%s，游标推进到第%d页'
                        % (page, total_page, format_size(downloaded_bytes()), cursor))

    if _downloaded_seqs:
        seq_list = sorted(_downloaded_seqs)
        with open("./run_seq.txt", "w", encoding="utf-8") as f:
            f.write(f"{seq_list[0]} {seq_list[-1]}\n")
        logger.info("本次下载编号区间：%06d - %06d（共%d本）"
                    % (seq_list[0], seq_list[-1], len(seq_list)))
    else:
        with open("./run_seq.txt", "w", encoding="utf-8") as f:
            f.write("0 0\n")
        logger.info("本次没有下载任何漫画")
    logger.info("本次任务下载完毕，共下载%s" % format_size(downloaded_bytes()))