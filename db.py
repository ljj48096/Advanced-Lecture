"""SQLite 持久化：记录课程的通知/抢课状态，程序重启不丢失。"""
import sqlite3

DB_PATH = "course_status.db"


def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS course_record (
            course_uid  TEXT PRIMARY KEY,
            course_name TEXT,
            weekday     INTEGER,
            notified    INTEGER DEFAULT 0,
            grabbed     INTEGER DEFAULT 0,
            notify_time TEXT,
            grab_time   TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_notified(uid):
    conn = _conn()
    row = conn.execute("SELECT notified FROM course_record WHERE course_uid=?", (uid,)).fetchone()
    conn.close()
    return bool(row and row[0])


def mark_notified(uid, name, weekday):
    conn = _conn()
    conn.execute("""
        INSERT INTO course_record (course_uid, course_name, weekday, notified, notify_time)
        VALUES (?,?,?,1,datetime('now','localtime'))
        ON CONFLICT(course_uid) DO UPDATE SET notified=1, notify_time=datetime('now','localtime')
    """, (uid, name, weekday))
    conn.commit()
    conn.close()


def is_grabbed(uid):
    conn = _conn()
    row = conn.execute("SELECT grabbed FROM course_record WHERE course_uid=?", (uid,)).fetchone()
    conn.close()
    return bool(row and row[0])


def mark_grabbed(uid, name, weekday):
    conn = _conn()
    conn.execute("""
        INSERT INTO course_record (course_uid, course_name, weekday, grabbed, grab_time)
        VALUES (?,?,?,1,datetime('now','localtime'))
        ON CONFLICT(course_uid) DO UPDATE SET grabbed=1, grab_time=datetime('now','localtime')
    """, (uid, name, weekday))
    conn.commit()
    conn.close()


def is_failed(uid):
    """终态失败（满/无权/人脸等），grabbed=-1，不再重试。"""
    conn = _conn()
    row = conn.execute("SELECT grabbed FROM course_record WHERE course_uid=?", (uid,)).fetchone()
    conn.close()
    return bool(row and row[0] == -1)


def mark_failed(uid, name, weekday):
    conn = _conn()
    conn.execute("""
        INSERT INTO course_record (course_uid, course_name, weekday, grabbed, grab_time)
        VALUES (?,?,?,-1,datetime('now','localtime'))
        ON CONFLICT(course_uid) DO UPDATE SET grabbed=-1, grab_time=datetime('now','localtime')
    """, (uid, name, weekday))
    conn.commit()
    conn.close()
