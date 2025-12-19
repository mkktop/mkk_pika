import hmac
import hashlib
import requests
import json
from util import *
from time import time
from urllib.parse import urlencode
from logger import  logger

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
def episodes_all(book_id, title: str) -> list:
    """获取本子的全部章节"""
    try:
        first_page_data = episodes(book_id, current_page=1).json()
        if 'data' not in first_page_data:
            return []
        # 'total' represents the total number of chapters in the comic,
        # while 'pages' indicates the number of pages needed to paginate the chapter data.
        total_pages    = first_page_data["data"]["eps"]["pages"]
        total_episodes = first_page_data["data"]["eps"]["total"]
        episode_list  = list(first_page_data["data"]["eps"]["docs"])
        while total_pages > 1:
            additional_episodes = episodes(book_id, total_pages).json()["data"]["eps"]["docs"]
            episode_list.extend(list(additional_episodes))
            total_pages -= 1
        episode_list = sorted(episode_list, key=lambda x: x['order'])
        if len(episode_list) != total_episodes:
            raise Exception('wrong number of episodes,expect:' +
                total_episodes + ',actual:' + len(episode_list)
            )
    except KeyError as e:
        print(f"Comic {title} has been MISSING. KeyError: {e}")
        return []
    except Exception as e:
        print(f"An error occurred while fetching episodes for comic {title}. Error: {e}")
        return []
    return episode_list

# 根据章节获取图片
def picture(book_id, ep_id, page=1):
    url = f"{api_base}comics/{book_id}/order/{ep_id}/pages?page={page}"
    return http_do("GET", url=url)

# 获取本子详细信息
def comic_info(book_id):
    url = f"{api_base}comics/{book_id}"
    res = http_do("GET", url=url)
    return json.loads(res.content.decode())
