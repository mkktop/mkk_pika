import json
import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional
from logger import  logger

class ComicSQLiteDB:
    """SQLite漫画数据库操作类"""
    def __init__(self, db_path: str = "./data/comic_spider.db"):
        self.db_path = db_path
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)  # exist_ok=True 避免重复创建报错
            logger.info(f"✅ 自动创建文件夹：{db_dir}")
        self.conn: sqlite3.Connection = None
        self.cursor: sqlite3.Cursor = None
        self._connect()
        self._init_table()

    def _connect(self):
        """连接数据库（自动创建文件）"""
        try:
            self.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False  # 允许多线程（爬虫常用）
            )
            self.cursor = self.conn.cursor()
            # 优化写入性能（个人爬虫场景）
            self.conn.execute("PRAGMA synchronous = OFF")
            self.conn.execute("PRAGMA journal_mode = WAL")  # 读写并发
        except sqlite3.Error as e:
            raise Exception(f"数据库连接失败：{e}")
    def _init_table(self):
        """初始化表结构（不存在则创建）"""
        create_sql = '''
        CREATE TABLE IF NOT EXISTS comic_info (
            comic_id TEXT NOT NULL PRIMARY KEY,
            title TEXT DEFAULT NULL,
            author TEXT DEFAULT '',
            finished BOOLEAN DEFAULT FALSE,
            pagesCount INTEGER DEFAULT 0,
            category TEXT DEFAULT '',
            epsCount INTEGER DEFAULT 0,
            update_time TEXT DEFAULT NULL,
            downloaded_episodes TEXT DEFAULT NULL,
            crawl_time TEXT DEFAULT (datetime('now')),
            CONSTRAINT idx_comic_title UNIQUE (title, author)
        )
        '''
        create_index_sqls = [
            "CREATE INDEX IF NOT EXISTS idx_comic_category ON comic_info(category)",
            "CREATE INDEX IF NOT EXISTS idx_comic_update_time ON comic_info(update_time)"
        ]
        try:
            self.cursor.execute(create_sql)
            # 执行建索引
            for sql in create_index_sqls:
                self.cursor.execute(sql)
            self.conn.commit()
            logger.info("表和索引初始化成功")
        except sqlite3.Error as e:
            self.conn.rollback()
            logger.error(f"表或索引初始化失败：{e}")
            raise Exception(f"表初始化失败：{e}")

    def save_comic(self, comic_data: Dict):
        """
        保存单条漫画数据（存在则更新指定字段，不存在则插入，不覆盖downloaded_episodes）
        :param comic_data: 字典，必须包含comic_id，其他字段可选
        """
        # 字段列表（排除 downloaded_episodes，避免覆盖）
        fields = [
            "comic_id", "title", "author", "finished", "pagesCount", "category", "epsCount",
            "update_time"
        ]
        # 过滤数据，只保留表中存在的字段
        filtered_data = {k: v for k, v in comic_data.items() if k in fields}
        if "comic_id" not in filtered_data:
            raise ValueError("comic_data 必须包含 comic_id 字段")

        # 补全默认值
        for field in fields:
            filtered_data.setdefault(field, '')

        # 构造 UPSERT SQL（存在则更新，只更新指定字段，保留 downloaded_episodes）
        columns = ', '.join(filtered_data.keys())
        placeholders = ', '.join(['?'] * len(filtered_data))
        # 需要更新的字段（排除主键 comic_id）
        update_fields = [f"{k} = excluded.{k}" for k in filtered_data.keys() if k != "comic_id"]
        update_clause = ', '.join(update_fields)

        sql = f'''
        INSERT INTO comic_info ({columns})
        VALUES ({placeholders})
        ON CONFLICT(comic_id) DO UPDATE SET {update_clause}
        '''
        try:
            self.cursor.execute(sql, tuple(filtered_data.values()))
            self.conn.commit()
            logger.info(f"保存成功：{filtered_data.get('title', '未知标题')}")
        except sqlite3.Error as e:
            self.conn.rollback()
            raise Exception(f"保存失败：{e} | 数据：{filtered_data}")

    def get_comic(self,
                  comic_id: Optional[str] = None,
                  title: Optional[str] = None,
                  category: Optional[str] = None,
                  limit: int = 100) -> List[Dict]:
        """
        灵活查询漫画数据（支持多条件组合）
        :param comic_id: 精确查询漫画ID
        :param title: 模糊查询标题
        :param category: 模糊查询分类
        :param limit: 返回结果数量限制
        :return: 字典列表（字段名: 值）
        """
        # 构造查询条件
        conditions = []
        params = []
        if comic_id:
            conditions.append("comic_id = ?")
            params.append(comic_id)
        if title:
            conditions.append("title LIKE ?")
            params.append(f"%{title}%")
        if category:
            conditions.append("category LIKE ?")
            params.append(f"%{category}%")

        # 拼接SQL
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT * FROM comic_info {where_clause} LIMIT ?"
        params.append(limit)

        try:
            self.cursor.execute(sql, params)
            # 转换为字典列表（更易用）
            columns = [desc[0] for desc in self.cursor.description]
            results = [dict(zip(columns, row)) for row in self.cursor.fetchall()]
            logger.info(f"查询完成：共{len(results)}条结果")
            return results
        except sqlite3.Error as e:
            raise Exception(f"查询失败：{e}")

    def delete_comic(self, comic_id: str) -> bool:
        """
        根据ID删除漫画数据
        :param comic_id: 漫画唯一ID
        :return: 是否删除成功
        """
        sql = "DELETE FROM comic_info WHERE comic_id = ?"
        try:
            self.cursor.execute(sql, (comic_id,))
            self.conn.commit()
            if self.cursor.rowcount > 0:
                logger.info(f"删除成功：{comic_id}")
                return True
            else:
                logger.info(f"删除失败：未找到ID为{comic_id}的漫画")
                return False
        except sqlite3.Error as e:
            self.conn.rollback()
            raise Exception(f"删除失败：{e}")

    def close(self):
        """关闭数据库连接"""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()
        logger.info("数据库连接已关闭")

    def __del__(self):
        """析构函数：自动关闭连接"""
        self.close()

    def get_downloaded_comic_count(self):
        """
        获取已下载漫画的数量。
        """
        self.cursor.execute('SELECT COUNT(*) FROM comic_info')
        count = self.cursor.fetchone()[0]
        return count

    def is_comic_downloaded(self,cid):
        """
        检查漫画 ID 是否已经下载过。
        """
        self.cursor.execute('SELECT 1 FROM comic_info WHERE comic_id = ?', (cid,))
        result = self.cursor.fetchone()
        return result is not None

    def is_episode_downloaded(self,comic_id,episode_title):
        """
        判断漫画的指定章节是否已下载。
        """
        self.cursor.execute('SELECT downloaded_episodes FROM comic_info WHERE comic_id = ?', (comic_id,))
        result = self.cursor.fetchone()
        if result and result[0]:
            downloaded_episodes = json.loads(result[0])
            logger.info("查询数据库中是否已存在该章节")
            logger.info(downloaded_episodes)
            return episode_title in downloaded_episodes
        return False

    def mark_comic_as_downloaded(self,comic_id):
        """
        标记漫画为已下载，在数据库中插入该 comic_id。
        """
        self.cursor.execute('SELECT comic_id FROM comic_info WHERE comic_id = ?', (comic_id,))
        result = self.cursor.fetchone()
        if not result:
            self.cursor.execute('INSERT OR IGNORE INTO comic_info (comic_id) VALUES (?)', (comic_id,))
            logger.info("数据库插入该漫画ID")

    def update_downloaded_episodes(self,comic_id,episode_title):
        """
        更新数据库中的已下载章节列表。
        """
        #获取已经下载的章节
        self.cursor.execute('SELECT downloaded_episodes FROM comic_info WHERE comic_id = ?', (comic_id,))
        result = self.cursor.fetchone()
        # 如果该漫画已存在，获取已下载章节列表
        if result and result[0]:
            downloaded_episodes = json.loads(result[0])
        else:
            downloaded_episodes = []
        downloaded_episodes.append(episode_title)
        # 更新数据库中的章节列表
        self.cursor.execute('''
            UPDATE comic_info 
            SET downloaded_episodes = ? 
            WHERE comic_id = ?
            ''', (json.dumps(downloaded_episodes), comic_id))
        self.conn.commit()
        logger.info("数据库更新已下载章节")
        #从数据库里获取章节列表打印出来
        self.cursor.execute('SELECT downloaded_episodes FROM comic_info WHERE comic_id = ?', (comic_id,))
        result = self.cursor.fetchone()
        if result and result[0]:
            downloaded_episodes = json.loads(result[0])
            logger.info("该漫画已下载章节如下")
            logger.info(downloaded_episodes)


