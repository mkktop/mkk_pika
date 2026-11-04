import hmac
import hashlib
import requests
import json
from util import *
from time import time, sleep
from urllib.parse import urlencode
from logger import  logger
from urllib.parse import quote
api_base = "https://picaapi.picacomic.com/"

headers = {
    "api-key": "C69BAF41DA5ABD1FFEDC6D2FEA56B",
    "accept": "application/vnd.picacomic.com.v1+json",
    "app-channel": "1",
    "nonce": "b1ab87b4800d4d4590a11701b8551afa",
    "app-version": "2.2.1.2.3.3",
    "app-uuid": "defaultUuid",
    "app-platform": "android",
    "app-build-version": "45",
    "Content-Type": "application/json; charset=UTF-8",
    "User-Agent": "okhttp/3.8.1",
    "image-quality": "original"
}

Order_Default = "ua"  # 默认
Order_Latest = "dd"  # 新到旧
Order_Oldest = "da"  # 旧到新
Order_Loved = "ld"  # 最多爱心
Order_Point = "vd"  # 最多指名

def http_do(method, url, **kwargs):
    """
    执行HTTP请求到API接口。

    此函数自动生成API请求所需的签名，并添加必要的认证头信息。
    默认禁用SSL证书验证，使用时需注意安全风险。
    参数:
    ----------
    method : str
        HTTP请求方法，如 'GET', 'POST', 'PUT', 'DELETE' 等。
    url : str
        请求的完整URL地址。如果URL包含api_base前缀，签名时会自动移除该前缀。
    **kwargs : dict, 可选
        传递给requests.request的其他参数，如：
        - headers: dict, 额外的HTTP头信息
        - params: dict, URL查询参数
        - data: dict, 请求体数据
        - json: dict, JSON格式的请求体数据
        - allow_redirects: bool, 是否允许重定向，默认为True
        - timeout: int/float, 请求超时时间，默认为10秒
    返回:
    -------
    requests.Response
        包含服务器响应的Response对象。
    """
    kwargs.setdefault("allow_redirects", True)
    header = headers.copy()
    ts = str(int(time()))
    raw = url.replace(api_base, "") + str(ts) + header["nonce"] + method + header["api-key"]
    secret_key = r"~d}$Q7$eIni=V)9\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"
    hc = hmac.new(secret_key.encode(), digestmod=hashlib.sha256)
    hc.update(raw.lower().encode())
    header["signature"] = hc.hexdigest()
    header["time"] = ts
    kwargs.setdefault("headers", header)
    proxies = None #代理
    response = requests.request(method = method, url = url,verify=False,proxies = proxies,timeout = 10, **kwargs)
    return response

def api_get_json(url, retries=3, wait=1):
    """带重试地请求 JSON 接口。
    网络异常、HTTP 非 200、业务 code 非 200、缺少 data 字段都会重试。
    全部失败返回 None。"""
    last_error = None
    for attempt in range(retries):
        try:
            resp = http_do("GET", url=url)
            data = resp.json()
            if resp.status_code == 200 and data.get("code", 200) == 200 and "data" in data:
                return data
            last_error = (f"status={resp.status_code} code={data.get('code')} "
                          f"message={data.get('message')}")
        except Exception as e:
            last_error = repr(e)
        if attempt < retries - 1:
            delay = wait * (attempt + 1)
            logger.warning(f"请求失败({attempt + 1}/{retries})，{delay}秒后重试：{url} | {last_error}")
            sleep(delay)
    logger.error(f"请求{retries}次均失败：{url} | {last_error}")
    return None

def login():
    """登录API"""
    url = api_base + "auth/sign-in"
    email = get_config(section="global", key="USER_NAME")
    password = get_config(section="global", key="USER_PASSWORD")
    send = {
        "email": email,
        "password": password
    }
    response = http_do("POST", url=url, json=send).text

    if json.loads(response)["code"] != 200:
        raise Exception('PICA_ACCOUNT/PICA_PASSWORD ERROR')
    if 'token' not in response:
        raise Exception('PICA_SECRET_KEY ERROR')
    headers["authorization"] = json.loads(response)["data"]["token"]
    logger.info(f"登录成功")

def punch_in():
    """打卡API"""
    url = f"{api_base}/users/punch-in"
    res = http_do("POST", url=url)
    logger.info(f"执行打卡任务")
    return  json.loads(res.content.decode())

def leaderboard():
    """排行榜API"""
    args = [("tt", 'H24'), ("ct", 'VC')]
    params = urlencode(args)
    url = f"{api_base}comics/leaderboard?{params}"
    res = http_do("GET", url)
    return json.loads(res.content.decode("utf-8"))["data"]["comics"]

def my_favourite(page=1):
    """获取某页收藏夹API"""
    url = f"{api_base}users/favourite?page={page}"
    res = http_do("GET", url=url)
    return json.loads(res.content.decode())["data"]["comics"]

def my_favourite_all():
    """获取全部收藏夹"""
    comics = []
    pages = my_favourite()["pages"]
    for page in range(1, pages + 1):
        comics += my_favourite(page)["docs"]
    return comics

def favourite(book_id):
    """收藏/取消收藏本子"""
    url = f"{api_base}comics/{book_id}/favourite"
    return http_do("POST", url=url)

def episodes(book_id, current_page):
    """获取本子的章节 一页最大40条"""
    url = f"{api_base}comics/{book_id}/eps?page={current_page}"
    return http_do("GET", url=url)

#
def episodes_all(book_id, title: str):
    """获取本子的全部章节。
    成功返回章节列表（可能为空列表），获取失败返回 None（调用方应重试而不是当成没有章节）。"""
    try:
        first_page_data = api_get_json(f"{api_base}comics/{book_id}/eps?page=1")
        if not first_page_data:
            logger.error(f"获取{title}章节失败")
            return None
        # 'total' represents the total number of chapters in the comic,
        # while 'pages' indicates the number of pages needed to paginate the chapter data.
        eps = first_page_data["data"]["eps"]
        total_pages    = eps["pages"]
        total_episodes = eps["total"]
        episode_list  = list(eps["docs"])
        for page in range(2, total_pages + 1):
            page_data = api_get_json(f"{api_base}comics/{book_id}/eps?page={page}")
            if not page_data:
                logger.error(f"获取{title}章节第{page}页失败")
                return None
            episode_list.extend(page_data["data"]["eps"]["docs"])
        episode_list = sorted(episode_list, key=lambda x: x['order'])
        if len(episode_list) != total_episodes:
            logger.error(f"{title}章节数不一致，expect:{total_episodes},actual:{len(episode_list)}")
            return None
    except KeyError as e:
        logger.error(f"{title}章节数据缺少字段（可能已下架）：{e}")
        return None
    except Exception as e:
        logger.error(f"获取{title}章节异常：{e}")
        return None
    return episode_list

# 根据章节获取图片（带重试，失败抛异常由调用方处理）
def picture(book_id, ep_id, page=1):
    url = f"{api_base}comics/{book_id}/order/{ep_id}/pages?page={page}"
    data = api_get_json(url)
    if data is None:
        raise Exception(f"获取图片列表失败：{book_id}/{ep_id}/page={page}")
    return data["data"]["pages"]["docs"]

# 获取本子详细信息（带重试）
def comic_info(book_id):
    url = f"{api_base}comics/{book_id}"
    data = api_get_json(url)
    if data is None:
        raise Exception(f"获取漫画详情失败：{book_id}")
    return data

def get_categories():
    url = f"{api_base}categories"
    res = http_do("GET", url=url)
    return json.loads(res.content.decode("utf-8"))

def search(keyword, page=1, sort=Order_Latest):
    url = f"{api_base}comics/advanced-search?page={page}"
    res = http_do("POST", url=url, json={"keyword": keyword, "sort": sort})
    return json.loads(res.content.decode("utf-8"))["data"]["comics"]

#通过分类获取漫画
def categories_search(page, categories, sort=Order_Latest):
    """
    分类搜索
    :param page: 页码
    :param categories: 分类
    :param sort: 排序方式
    :return: 搜索结果
    """
    categories = quote(categories)
    url = f"{api_base}comics?page={page}&c={categories}&s={sort}"
    res = http_do("GET", url=url)
    return json.loads(res.content.decode("utf-8"))["data"]["comics"]

#获取最新漫画
def get_update(page):
    sort = Order_Latest
    url = f"{api_base}comics?page={page}&s={sort}"
    res = http_do("GET", url=url)
    return json.loads(res.content.decode("utf-8"))["data"]["comics"]

#从旧到新获取漫画
def get_old_update(page):
    sort = Order_Oldest
    url = f"{api_base}comics?page={page}&s={sort}"
    res = http_do("GET", url=url)
    return json.loads(res.content.decode("utf-8"))["data"]["comics"]
