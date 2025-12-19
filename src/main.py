import os
from datetime import datetime
import urllib3
from time import sleep
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from logger import  logger
from api import *
from database import ComicSQLiteDB

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
    cid = comic["_id"]
    title = comic["title"]
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
    else:
        return
    #数据库加入该漫画
    db.mark_comic_as_downloaded(comic["_id"])
    comic_path = os.path.join("..",
                              "comics",
                              f"{convert_file_name(title)}"
                            )
    comic_path = ensure_valid_path(comic_path)
    for episode in episodes:
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
        futures = {
            executor.submit(download,name_len,
                            chapter_path,
                            image_urls.index(image_url),image_url,
                            ): image_url
            for image_url in image_urls
        }
        for future in as_completed(futures):
            image_url = futures[future]
            try:
                future.result()
                downloaded_count += 1
            except Exception as e:
                current_image = image_urls.index(image_url) + 1
                episode_title = episode["title"]
                logger.error(f"Error downloading the {current_image}-th image"
                          f"in episode:{episode_title}"
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
        else:
            episode_title = episode["title"]
            logger.error(
                f"Failed to download the episodes:{episode_title} "
                f"of comic:{title}. "
                f"Currently, {downloaded_count} images(total_images:{len(image_urls)}) "
                "from this episode have been downloaded"
            )
        sleep(1)



if __name__ == "__main__":
    #初始化数据库
    db = ComicSQLiteDB("../data/comic_spider.db")
    #获取累计的下载数量
    logger.info('已经累计下载%d本漫画' %db.get_downloaded_comic_count())
    #登录
    login()
    #获取收藏夹的漫画数量
    favourite_comics = my_favourite_all()
    #获取下载的线程数
    logger.info('收藏夹共计%d本漫画' % (len(favourite_comics)))
    thread_number = get_config(section="download", key="thread_number")

    with ThreadPoolExecutor(max_workers=thread_number) as executor:
        for the_comic in favourite_comics:
            try:
                #开始下载
                download_comic(the_comic,executor)
                info = comic_info(the_comic['_id'])
                data = info["data"]['comic']
                is_remove_favorites = get_config(section="download", key="remove_favorites")
                if is_remove_favorites and data['isFavourite']:
                    favourite(data["_id"])
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
                add_comic ={
                    "comic_id": comic_id,
                    "title": title,
                    "author": author,
                    "finished": finished,
                    "pagesCount": pagesCount,
                    "category": category,
                    "epsCount": epsCount,
                    "update_time": update_time,
                }
                db.save_comic(add_comic)

            except Exception as e:
                logger.error(
                    'Download failed for {}, with Exception:{}'.format(the_comic["title"], e)
                )
                continue


