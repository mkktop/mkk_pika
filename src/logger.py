import logging
import re
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
import os

def setup_rotating_logger():
    # 1. 创建日志器（命名为项目名，避免与其他日志器冲突）
    logger = logging.getLogger("mkk_pika")
    logger.setLevel(logging.DEBUG)  # 日志器总级别（需≤处理器级别）
    logger.propagate = False  # 防止日志向上传播（避免重复输出）

    # 清空已有Handler（防止重复配置）
    if logger.handlers:
        logger.handlers.clear()

    # 2. 确保日志目录存在
    log_dir = "./logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f'data_{datetime.now().strftime("%Y-%m-%d")}.log')

    # 3. 创建按时间轮转的文件Handler（核心配置）
    # when='D'：按天轮转；interval=1：每1天轮转一次；backupCount=7：保留7个备份（即7天日志）
    file_handler = TimedRotatingFileHandler(
        filename=log_file,          # 基础日志文件名
        when='D',                   # 轮转单位：D(天)、H(小时)、M(分钟)、S(秒)
        interval=1,                 # 每1天轮转一次
        backupCount=7,              # 保留7个备份文件（超过自动删除）
        encoding='utf-8',           # 中文编码，避免乱码
        delay=False,                # 立即创建日志文件
        utc=False                   # 使用本地时间（True则用UTC时间）
    )

    # 可选：自定义轮转文件名（默认格式：app.log.2025-12-17）
    file_handler.suffix = "%Y-%m-%d"  # 轮转文件后缀（按日期命名）
    # 过滤非日期后缀的文件（避免删除其他文件）
    file_handler.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}(\.\w+)?$")

    # 4. 创建控制台Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)  # 控制台只输出INFO及以上

    # 5. 定义日志格式（包含时间、模块、行号等关键信息）
    log_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(filename)s:%(lineno)d - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(log_formatter)
    console_handler.setFormatter(log_formatter)

    # 6. 将Handler添加到日志器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

# 初始化日志器（全局单例，其他文件导入时直接用）
logger = setup_rotating_logger()