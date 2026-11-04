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
        create_retry_sql = '''
        CREATE TABLE IF NOT EXISTS comic_retry (
            comic_id TEXT NOT NULL PRIMARY KEY,
            attempts INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        )
        '''
        create_chapter_sql = '''
        CREATE TABLE IF NOT EXISTS chapter (
            comic_id TEXT NOT NULL,
            ep_order TEXT NOT NULL,
            ep_id TEXT DEFAULT '',
            title TEXT DEFAULT '',
            PRIMARY KEY (comic_id, ep_order)
        )
        '''
        try:
            self.cursor.execute(create_sql)
            # 执行建索引
            for sql in create_index_sqls:
                self.cursor.execute(sql)
            # 下载失败计数表，用于避免坏数据无限卡住分页游标
            self.cursor.execute(create_retry_sql)
            # 章节信息表，用于网站失效后本地还原章节名
            self.cursor.execute(create_chapter_sql)
            # 旧库补字段（幂等）
            for column, ddl in [
                ("seq", "seq INTEGER"),
                ("description", "description TEXT DEFAULT ''"),
                ("tags", "tags TEXT DEFAULT ''"),
            ]:
                self._ensure_column("comic_info", column, ddl)
            self._backfill_seq()
            # seq 字段补好之后才能建唯一索引
            self.cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_comic_seq ON comic_info(seq)"
            )
            self.conn.commit()
            logger.info("表和索引初始化成功")
        except sqlite3.Error as e:
            self.conn.rollback()
            logger.error(f"表或索引初始化失败：{e}")
            raise Exception(f"表初始化失败：{e}")

    def _ensure_column(self, table: str, column: str, ddl: str):
        """表里没有该字段时补上，用于旧库平滑升级"""
        self.cursor.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in self.cursor.fetchall()}
        if column not in columns:
            self.cursor.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            logger.info(f"表{table}新增字段{column}")

    def _backfill_seq(self):
        """给还没有编号的历史漫画按入库顺序补编号"""
        self.cursor.execute('SELECT COALESCE(MAX(seq), 0) FROM comic_info')
        start = self.cursor.fetchone()[0]
        self.cursor.execute(
            'SELECT comic_id FROM comic_info WHERE seq IS NULL ORDER BY crawl_time, comic_id'
        )
        rows = self.cursor.fetchall()
        if not rows:
            return
        for offset, (comic_id,) in enumerate(rows, start=1):
            self.cursor.execute(
                'UPDATE comic_info SET seq = ? WHERE comic_id = ?', (start + offset, comic_id)
            )
        logger.info(f"为{len(rows)}本历史漫画补编号：{start + 1} ~ {start + len(rows)}")

    def save_comic(self, comic_data: Dict):
        """
        保存单条漫画数据（存在则更新指定字段，不存在则插入，不覆盖downloaded_episodes）
        :param comic_data: 字典，必须包含comic_id，其他字段可选
        """
        # 字段列表（排除 downloaded_episodes，避免覆盖）
        fields = [
            "comic_id", "title", "author", "finished", "pagesCount", "category", "epsCount",
            "update_time", "description", "tags"
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

    def is_episode_downloaded(self,comic_id,episode_key):
        """
        判断漫画的指定章节是否已下载。
        章节用 order 作为唯一标识，避免同名章节互相覆盖。
        """
        self.cursor.execute('SELECT downloaded_episodes FROM comic_info WHERE comic_id = ?', (comic_id,))
        result = self.cursor.fetchone()
        if result and result[0]:
            downloaded_episodes = json.loads(result[0])
            return episode_key in downloaded_episodes
        return False

    def mark_comic_as_downloaded(self,comic_id):
        """
        确保漫画已在库中，并返回它的编号（从1开始自增）。
        """
        self.cursor.execute('SELECT seq FROM comic_info WHERE comic_id = ?', (comic_id,))
        result = self.cursor.fetchone()
        if result and result[0] is not None:
            return result[0]
        self.cursor.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM comic_info')
        seq = self.cursor.fetchone()[0]
        if result:
            # 已有行但没有编号（历史数据兜底）
            self.cursor.execute('UPDATE comic_info SET seq = ? WHERE comic_id = ?', (seq, comic_id))
        else:
            self.cursor.execute(
                'INSERT OR IGNORE INTO comic_info (comic_id, seq) VALUES (?, ?)', (comic_id, seq)
            )
            logger.info(f"第一次下载该漫画，记录数据库中，编号{seq}")
        self.conn.commit()
        return seq

    def update_downloaded_episodes(self,comic_id,episode_key):
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
        if episode_key not in downloaded_episodes:
            downloaded_episodes.append(episode_key)
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

    def save_chapters(self, comic_id: str, episodes: list):
        """保存漫画的全部章节信息（order/title/ep_id），供网站失效后本地还原"""
        if not episodes:
            return
        rows = [
            (comic_id, str(e.get("order", "")), str(e.get("_id", "")), str(e.get("title", "")))
            for e in episodes
        ]
        self.cursor.executemany('''
            INSERT INTO chapter (comic_id, ep_order, ep_id, title)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(comic_id, ep_order) DO UPDATE SET
                ep_id = excluded.ep_id,
                title = excluded.title
            ''', rows)
        self.conn.commit()

    def get_chapters(self, comic_id: str) -> List[Dict]:
        """查询漫画的章节列表，按 order 排序"""
        self.cursor.execute('''
            SELECT ep_order, ep_id, title FROM chapter
            WHERE comic_id = ?
            ORDER BY CAST(ep_order AS INTEGER), ep_order
            ''', (comic_id,))
        return [
            {"order": row[0], "ep_id": row[1], "title": row[2]}
            for row in self.cursor.fetchall()
        ]

    def add_comic_failure(self, comic_id: str, error: str = '') -> int:
        """记录一次下载失败，返回累计失败次数"""
        self.cursor.execute('''
            INSERT INTO comic_retry (comic_id, attempts, last_error, updated_at)
            VALUES (?, 1, ?, datetime('now'))
            ON CONFLICT(comic_id) DO UPDATE SET
                attempts = attempts + 1,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            ''', (comic_id, str(error)[:500]))
        self.conn.commit()
        return self.get_comic_failure_count(comic_id)

    def clear_comic_failure(self, comic_id: str):
        """下载成功后清除失败记录"""
        self.cursor.execute('DELETE FROM comic_retry WHERE comic_id = ?', (comic_id,))
        self.conn.commit()

    def get_comic_failure_count(self, comic_id: str) -> int:
        """查询该漫画连续失败次数"""
        self.cursor.execute('SELECT attempts FROM comic_retry WHERE comic_id = ?', (comic_id,))
        result = self.cursor.fetchone()
        return result[0] if result else 0

    def get_title_by_comic_id(self, comic_id: str) -> Optional[str]:
        """
        根据漫画ID查询标题
        :param comic_id: 漫画唯一ID
        :return: 漫画标题，如果未找到则返回None
        """
        sql = "SELECT title FROM comic_info WHERE comic_id = ?"
        try:
            self.cursor.execute(sql, (comic_id,))
            result = self.cursor.fetchone()
            if result:
                logger.info(f"根据ID查询到标题：{comic_id} -> {result[0]}")
                return result[0]
            else:
                logger.info(f"未找到ID为{comic_id}的漫画标题")
                return None
        except sqlite3.Error as e:
            raise Exception(f"查询失败：{e}")

    def create_download_all_info(self):
        """初始化表结构（不存在则创建）"""
        create_sql = '''
        CREATE TABLE IF NOT EXISTS download_info (
            comic_id TEXT NOT NULL PRIMARY KEY,
            pagesCount INTEGER DEFAULT 0,
            totalPages INTEGER DEFAULT 0,
            total_comic INTEGER DEFAULT 0,
            crawl_time TEXT DEFAULT (datetime('now'))
        )
        '''
        try:
            self.cursor.execute(create_sql)
            self.conn.commit()
            logger.info("表和索引初始化成功")
        except sqlite3.Error as e:
            self.conn.rollback()
            logger.error(f"表或索引初始化失败：{e}")
            raise Exception(f"表初始化失败：{e}")

    def save_download_info(self, comic_data: Dict):
        """
        保存单条漫画数据（存在则更新指定字段，不存在则插入）
        :param comic_data: 字典，必须包含comic_id，其他字段可选
        """
        # 字段列表（排除 downloaded_episodes，避免覆盖）
        fields = [
            "comic_id", "pagesCount", "total_comic", "totalPages"
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
        INSERT INTO download_info ({columns})
        VALUES ({placeholders})
        ON CONFLICT(comic_id) DO UPDATE SET {update_clause}
        '''
        try:
            self.cursor.execute(sql, tuple(filtered_data.values()))
            self.conn.commit()
            logger.info(f"保存成功")
        except sqlite3.Error as e:
            self.conn.rollback()
            raise Exception(f"保存失败：{e} | 数据：{filtered_data}")

    def get_download_info_pagesCount(self, comic_id: str):
        sql = "SELECT pagesCount FROM download_info WHERE comic_id = ?"
        try:
            self.cursor.execute(sql, (comic_id,))
            result = self.cursor.fetchone()
            if result:
                return result[0]
            else:
                return None
        except sqlite3.Error as e:
            raise Exception(f"查询失败：{e}")

    def mark_download_info_id(self,comic_id):
        self.cursor.execute('SELECT comic_id FROM download_info WHERE comic_id = ?', (comic_id,))
        result = self.cursor.fetchone()
        if not result:
            self.cursor.execute('INSERT OR IGNORE INTO download_info (comic_id) VALUES (?)', (comic_id,))
            logger.info("第一次下载全部漫画，记录数据库中")
            self.conn.commit()
