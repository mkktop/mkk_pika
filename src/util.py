import json
import yaml
import os
from datetime import datetime, timezone
from logger import  logger

CONFIG_PATH = "./config/comic.yaml"
max_path_length = 110

def generate_default_config():
    """生成默认配置文件（初次运行时自动创建）"""
    default_config = {
        "global": {
            "pdf_switch": 0,
            "pdf_password": "None"
        }
    }
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(default_config, f, indent=2, allow_unicode=True,sort_keys=False)
    logger.info(f"【首次运行】已自动生成默认配置文件 → {CONFIG_PATH}")
    logger.info("请修改配置文件后重新运行！")

def load_config():
    if not os.path.exists(CONFIG_PATH):
        generate_default_config()
        # 生成默认配置后，提示用户修改并退出（避免直接运行示例配置报错）
        exit(0)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def get_config(section: str, key: str, default_value = ''):
    """
    读取配置
    读取优先级为 环境变量 > config.ini > default_value默认值
    """
    #先读取环境变量
    config_value = os.environ.get(key.upper())
    if config_value:
        return config_value
    #从配置文件中寻找
    config = load_config()
    config = config[section]
    config_value = config[key]
    if config_value:
        return config_value
    #都没有返回默认值
    return default_value

def print_full_json(json_data):
    """打印完整的JSON响应"""
    if json_data:
        print("\n=== 完整JSON响应 ===")
        print(json.dumps(json_data, indent=2, ensure_ascii=False))
        print("=== JSON响应结束 ===")
    else:
        print("没有JSON数据可打印")

def truncate_string_by_bytes(s: str, max_bytes: int) -> str:
    """
    截断字符串，使其字节长度不超过max_bytes。
    确保不会在UTF-8多字节字符的中间截断。

    参数:
    s (str): 要截断的字符串。
    max_bytes (int): 字符串的最大字节长度。

    返回:
    str: 截断后的字符串。
    """
    # 如果字符串本身字节长度就小于等于限制，直接返回
    if len(s.encode('utf-8')) <= max_bytes:
        return s

    # 从末尾开始尝试截断，确保不会在字符中间截断
    truncated = s[:max_bytes]  # 先按字符数截断（安全起点）

    # 逐步减少字符直到字节数满足要求且不会截断字符
    while len(truncated.encode('utf-8')) > max_bytes:
        truncated = truncated[:-1]

    # 更高效的方法：直接从字节层面处理
    encoded = s.encode('utf-8')
    if len(encoded) <= max_bytes:
        return s

    # 截断字节，并确保不会在UTF-8字符中间截断
    truncated_bytes = encoded[:max_bytes]

    # 移除可能不完整的UTF-8字符的字节
    while truncated_bytes and truncated_bytes[-1] & 0b11000000 == 0b10000000:
        # 如果最后一个字节是UTF-8字符的中间字节（以10开头），则移除
        truncated_bytes = truncated_bytes[:-1]

    # 移除最后一个可能不完整的字符的所有字节
    # 查找最后一个完整字符的开始位置
    for i in range(1, 5):  # UTF-8字符最多4个字节
        if len(truncated_bytes) <= i:
            break
        # 检查字节是否是一个字符的开始字节
        byte = truncated_bytes[-i]
        # 计算字符开始的模式
        if (byte & 0b10000000) == 0b00000000:  # 单字节字符
            break
        elif (byte & 0b11100000) == 0b11000000:  # 2字节字符
            if i >= 2:
                break
        elif (byte & 0b11110000) == 0b11100000:  # 3字节字符
            if i >= 3:
                break
        elif (byte & 0b11111000) == 0b11110000:  # 4字节字符
            if i >= 4:
                break

    return truncated_bytes.decode('utf-8', 'ignore')

def convert_file_name(name: str) -> str:
    """
    转换文件名，处理特殊字符并确保长度在限制内。

    参数:
    name (str): 原始文件名。

    返回:
    str: 处理后的文件名。
    """
    if isinstance(name, list):
        name = "&".join(map(str, name))

    # 定义需要替换的字符对
    replacement_pairs = [
        ("/", "／"), ("\\", "＼"), ("?", "？"), ("|", "︱"),
        ("\"", "＂"), ("*", "＊"), ("<", "＜"), (">", "＞"),
        (":", "："), ("-", "－")
    ]

    # 一次性替换所有特殊字符
    for old, new in replacement_pairs:
        name = name.replace(old, new)

    # 移除空格
    name = name.replace(" ", "")

    # 操作系统对文件夹名最大长度有限制，这里对超长部分进行截断
    # Linux是255字节，Windows是260字符（但不同系统限制不同）
    name = truncate_string_by_bytes(name, 255)

    return name

def ensure_valid_path(path):
    if len(path) > max_path_length:
        print(f"Path too long, truncating: {path}")
        path = path[:max_path_length]  # 截断路径
    return path

def compare_time(start_time):
    time_str = start_time
    # 1. 解析字符串为UTC时区的datetime对象
    # %Y=年, %m=月, %d=日, %H=时, %M=分, %S=秒, %f=微秒
    target_time = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    # 3. 获取当前时间
    # 当前UTC时间
    current_utc_time = datetime.now(timezone.utc)
    logger.info(f"目标时间（UTC）：{target_time}")
    logger.info(f"当前UTC时间：{current_utc_time}")
    time_diff = current_utc_time - target_time
    logger.info(f"相差：{time_diff.days} 天 {time_diff.seconds // 3600} 小时 {(time_diff.seconds % 3600) // 60} 分钟")
    out_time_day = get_config("download","out_time_day", "30")
    out_time_day = int(out_time_day)
    if time_diff.days >= out_time_day :
        return 1
    return 0
