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

#下载漫画
def download_comic(comic,executor:ThreadPoolExecutor):
    if size_limit_reached():
        soft, _ = download_limits()
        logger.info(f"已达到本次下载上限({format_size(soft)})，跳过{comic.get('title')}")
        return
    cid = comic["_id"]
    title = comic["title"]
    logger.info(f"开始检查{title}是否存在未下载章节")
    author = comic["author"]
    categories = comic["categories"]
    episodes = episodes_all(cid, title)
    num_pages = comic["pagesCount"] if "pagesCount" in comic else -1
    is_detail = get_config("download","is_detail")
    if db.is_comic_downloaded(comic["_id"]):
        episodes = [episode for episode in episodes
                    if not db.is_episode_downloaded(comic["_id"], episode["title"])]
    if episodes:
        logger.info(
            '正在下载:[%s]-[%s]-[%s]-[total_pages:%d]' %
            (title, author, categories, num_pages)
        )
        logger.info(episodes)
    else:
        logger.info(f"{title}中没有可下载章节")
        return
    #数据库加入该漫画
    db.mark_comic_as_downloaded(comic["_id"])
    comic_path = os.path.join(".",
                              "comics",
                              f"{convert_file_name(title)}"
                            )
    comic_path = ensure_valid_path(comic_path)
    for episode in episodes:
        if hard_limit_reached():
            _, hard = download_limits()
            logger.info(f"已达到单本漫画硬上限({format_size(hard)})，剩余章节留待下次")
            return
        chapter_title = convert_file_name(episode["title"])
        chapter_path = os.path.join(comic_path, chapter_title)
        chapter_path = Path(chapter_path)
        chapter_path.mkdir(parents=True, exist_ok=True)
        image_urls = []
        current_page = 1
        while True:
            page_data = json.loads(
                picture(cid, episode["order"], current_page).content
            )["data"]["pages"]["docs"]
            # print_full_json(page_data)
            current_page += 1
            if page_data:
                image_urls.extend(list(map(
                    lambda i: i['media']['fileServer'] + '/static/' + i['media']['path'],
                    page_data
                )))
            else:
                break
        if not image_urls:
            logger.warning(f"{title}{chapter_title}没有找到图片")
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
            db.update_downloaded_episodes(comic["_id"], episode["title"])
        elif limit_hit:
            logger.info(
                f"达到单本漫画硬上限，{chapter_title}已下载"
                f"{downloaded_count}/{len(image_urls)}张，剩余留待下次"
            )
        else:
            episode_title = episode["title"]
            logger.error(
                f"Failed to download the episodes:{episode_title} "
                f"of comic:{title}. "
                f"Currently, {downloaded_count} images(total_images:{len(image_urls)}) "
                "from this episode have been downloaded"
            )
        sleep(1)


def search_all(the_keyword):
    subscribed_comics = []
    categories = get_config("download","filter").split(',')
    download_page = get_config("download","page")
    if the_keyword:
        db.mark_comic_as_downloaded(the_keyword)
        last_title = db.get_title_by_comic_id(the_keyword)
        this_title = ""
        total_pages_num = search(the_keyword)["pages"]
        if total_pages_num < download_page:
            download_page = total_pages_num
        for page in range(1, download_page+1):
            page_docs = search(the_keyword, page)["docs"]
            for idx, doc in enumerate(page_docs):
                if idx == 1 and page == 1:
                    this_title = doc["title"]
                if last_title == doc["title"]:
                    last_comic = {
                        "comic_id": the_keyword,
                        "title": this_title,
                        "author": "mkk"
                    }
                    db.save_comic(last_comic)
                    return subscribed_comics
                intersection = set(doc['categories']).intersection(set(categories))
                if len(intersection) == 0:
                    subscribed_comics.append(doc)
        last_comic ={
            "comic_id": the_keyword,
            "title": this_title,
            "author": "mkk"
        }
        db.save_comic(last_comic)
    return subscribed_comics

def download_all_comics():
    """扫描全站下一页，返回 (页码, 总页数, 总漫画数, 待下载漫画列表)。
    游标由调用方在整页下载完成后推进，避免中途中断导致漏掉未下载内容。"""
    all_comics = []
    categories = get_config("download", "filter").split(',')
    download_name = "old_to_nwe"
    db.mark_download_info_id(download_name)
    page = db.get_download_info_pagesCount(download_name) or 0
    res = get_old_update(1)
    total_page =  res["pages"]
    total_comic = res["total"]
    if page >= total_page:
        logger.info(f"已经扫描了{page}页，一共{total_page}页，全部分页已扫描完毕")
        return page, total_page, total_comic, []
    page += 1
    logger.info(f"已经下载了{page-1}页，一共{total_page}页，现在开始添加第{page}页")
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
    #获取收藏夹的漫画数量
    favourite_comics = my_favourite_all()
    #获取下载的线程数
    logger.info('收藏夹共计%d本漫画' % (len(favourite_comics)))
    thread_number = int(get_config(section="download", key="thread_number"))
    #开始下载收藏夹内容
    logger.info("开始下载收藏夹内未下载内容")
    with ThreadPoolExecutor(max_workers=thread_number) as executor:
        for the_comic in favourite_comics:
            if size_limit_reached():
                logger.info("已达到本次下载上限，收藏夹剩余内容留待下次")
                break
            try:
                #开始下载
                download_comic(the_comic,executor)
                data = record_comic_info(the_comic['_id'])
                is_remove_favorites = get_config(section="download", key="remove_favorites")

                #如果设置了下载取消收藏，并且是已经完结的作品，则取消收藏
                if is_remove_favorites and data['isFavourite'] and data["finished"]:
                    favourite(data["_id"])
                    logger.info(f"{data['title']}已经取消收藏！")
                #如果设置了下载取消收藏，没有完结，但长时间不更新则取消收藏
                if is_remove_favorites and data['isFavourite'] and data["finished"] == False:
                    if compare_time(data["updated_at"]):
                        favourite(data["_id"])
                        logger.info(f"长时间未更新{data['title']}已经取消收藏！")

            except Exception as e:
                logger.error(
                    'Download failed for {}, with Exception:{}'.format(the_comic["title"], e)
                )
                continue
    logger.info("收藏夹内容下载完毕")
    #开始下载订阅内容
    logger.info("开始下载订阅内容")
    searched_comics = []
    if not size_limit_reached():
        keywords = get_config(section='download', key='key_world').split(',')
        for keyword in keywords:
            if size_limit_reached():
                logger.info("已达到本次下载上限，剩余关键词留待下次")
                break
            searched_comic = search_all(keyword)
            logger.info(f"关键词{keyword}找到{len(searched_comic)}本漫画")
            searched_comics += searched_comic
    logger.info('订阅内容共计%d本漫画' % (len(searched_comics)))
    with ThreadPoolExecutor(max_workers=thread_number) as executor:
        for the_comic in searched_comics:
            if size_limit_reached():
                logger.info("已达到本次下载上限，订阅内容剩余部分留待下次")
                break
            try:
                #开始下载
                download_comic(the_comic,executor)
                record_comic_info(the_comic['_id'])
            except Exception as e:
                logger.error(
                    'Download failed for {}, with Exception:{}'.format(the_comic["title"], e)
                )
                continue
    logger.info("订阅内容内容下载完毕")
    logger.info("开始分批下载全部章节")
    db.create_download_all_info()
    download_plan = int(get_config(section="download", key="download_plan", default_value=0))
    if size_limit_reached():
        logger.info("已达到本次下载上限，跳过分批下载")
    elif download_plan > 0:
        # download_plan 是本次最多扫描的分页数，真正的停止条件是下载大小上限
        for download_num in range(download_plan):
            if size_limit_reached():
                logger.info("已达到本次下载上限，停止本次分批下载")
                break
            page, total_page, total_comic, the_all_comics = download_all_comics()
            if not the_all_comics and page >= total_page:
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
                    try:
                        # 开始下载
                        download_comic(the_comic, executor)
                        record_comic_info(the_comic['_id'])
                    except Exception as e:
                        logger.error(
                            'Download failed for {}, with Exception:{}'.format(the_comic["title"], e)
                        )
                        continue
            if hard_limit_reached():
                page_complete = False
            if not page_complete:
                logger.info('第%d页未下载完，游标不推进，下次从第%d页继续' % (page, page))
                break
            db.save_download_info({
                "comic_id": "old_to_nwe",
                "pagesCount": page,
                "total_comic": total_comic,
                "totalPages": total_page,
            })
            logger.info('第%d/%d页完成，本次已下载%s，游标推进到第%d页'
                        % (page, total_page, format_size(downloaded_bytes()), page))

    logger.info("本次任务下载完毕，共下载%s" % format_size(downloaded_bytes()))