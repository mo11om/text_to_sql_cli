"""
setup_db.py
-----------
建立並填充 college_2.db SQLite 資料庫。
若資料庫檔案已存在，則跳過建立步驟（idempotent 冪等操作）。

用法：
    python setup_db.py
"""

import sqlite3
import os

# ── 資料庫路徑 ─────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "college_2.db")

# ── DDL：建立所有資料表 ────────────────────────────────────────────────────────
DDL_STATEMENTS = [
    # 教室資料表
    """
    CREATE TABLE IF NOT EXISTS classroom (
        building    VARCHAR(15),
        room_number VARCHAR(7),
        capacity    NUMERIC(4,0),
        PRIMARY KEY (building, room_number)
    )
    """,
    # 科系資料表
    """
    CREATE TABLE IF NOT EXISTS department (
        dept_name   VARCHAR(20),
        building    VARCHAR(15),
        budget      NUMERIC(12,2),
        PRIMARY KEY (dept_name)
    )
    """,
    # 課程資料表
    """
    CREATE TABLE IF NOT EXISTS course (
        course_id   VARCHAR(8),
        title       VARCHAR(50),
        dept_name   VARCHAR(20),
        credits     NUMERIC(2,0),
        PRIMARY KEY (course_id),
        FOREIGN KEY (dept_name) REFERENCES department(dept_name)
    )
    """,
    # 教師資料表
    """
    CREATE TABLE IF NOT EXISTS instructor (
        ID          VARCHAR(5),
        name        VARCHAR(20) NOT NULL,
        dept_name   VARCHAR(20),
        salary      NUMERIC(8,2),
        PRIMARY KEY (ID),
        FOREIGN KEY (dept_name) REFERENCES department(dept_name)
    )
    """,
    # 時段資料表
    """
    CREATE TABLE IF NOT EXISTS time_slot (
        time_slot_id VARCHAR(4),
        day          VARCHAR(1),
        start_hr     NUMERIC(2) CHECK (start_hr >= 0 AND start_hr < 24),
        start_min    NUMERIC(2) CHECK (start_min >= 0 AND start_min < 60),
        end_hr       NUMERIC(2) CHECK (end_hr >= 0 AND end_hr < 24),
        end_min      NUMERIC(2) CHECK (end_min >= 0 AND end_min < 60),
        PRIMARY KEY (time_slot_id, day, start_hr, start_min)
    )
    """,
    # 開課資料表
    # 注意：time_slot_id 不設 FOREIGN KEY，因為 SQLite 不支援引用複合主鍵的部分欄位
    """
    CREATE TABLE IF NOT EXISTS section (
        course_id    VARCHAR(8),
        sec_id       VARCHAR(8),
        semester     VARCHAR(6) CHECK (semester IN ('Fall', 'Winter', 'Spring', 'Summer')),
        year         NUMERIC(4,0) CHECK (year > 1701 AND year < 2100),
        building     VARCHAR(15),
        room_number  VARCHAR(7),
        time_slot_id VARCHAR(4),
        PRIMARY KEY (course_id, sec_id, semester, year),
        FOREIGN KEY (course_id) REFERENCES course(course_id),
        FOREIGN KEY (building, room_number) REFERENCES classroom(building, room_number)
    )
    """,
    # 教師授課資料表
    """
    CREATE TABLE IF NOT EXISTS teaches (
        ID          VARCHAR(5),
        course_id   VARCHAR(8),
        sec_id      VARCHAR(8),
        semester    VARCHAR(6),
        year        NUMERIC(4,0),
        PRIMARY KEY (ID, course_id, sec_id, semester, year),
        FOREIGN KEY (course_id, sec_id, semester, year) REFERENCES section(course_id, sec_id, semester, year),
        FOREIGN KEY (ID) REFERENCES instructor(ID)
    )
    """,
    # 學生資料表
    """
    CREATE TABLE IF NOT EXISTS student (
        ID          VARCHAR(5),
        name        VARCHAR(20) NOT NULL,
        dept_name   VARCHAR(20),
        tot_cred    NUMERIC(3,0) CHECK (tot_cred >= 0),
        PRIMARY KEY (ID),
        FOREIGN KEY (dept_name) REFERENCES department(dept_name)
    )
    """,
    # 選課資料表
    """
    CREATE TABLE IF NOT EXISTS takes (
        ID          VARCHAR(5),
        course_id   VARCHAR(8),
        sec_id      VARCHAR(8),
        semester    VARCHAR(6),
        year        NUMERIC(4,0),
        grade       VARCHAR(2),
        PRIMARY KEY (ID, course_id, sec_id, semester, year),
        FOREIGN KEY (course_id, sec_id, semester, year) REFERENCES section(course_id, sec_id, semester, year),
        FOREIGN KEY (ID) REFERENCES student(ID)
    )
    """,
    # 導師資料表 (學生 → 教師)
    """
    CREATE TABLE IF NOT EXISTS advisor (
        s_ID        VARCHAR(5),
        i_ID        VARCHAR(5),
        PRIMARY KEY (s_ID),
        FOREIGN KEY (s_ID) REFERENCES student(ID),
        FOREIGN KEY (i_ID) REFERENCES instructor(ID)
    )
    """,
    # 先修課程資料表
    """
    CREATE TABLE IF NOT EXISTS prereq (
        course_id   VARCHAR(8),
        prereq_id   VARCHAR(8),
        PRIMARY KEY (course_id, prereq_id),
        FOREIGN KEY (course_id) REFERENCES course(course_id),
        FOREIGN KEY (prereq_id) REFERENCES course(course_id)
    )
    """,
]

# ── 種子資料 (Seed Data) ───────────────────────────────────────────────────────
SEED_DATA = {
    "classroom": [
        ("Packard",  "101", 500),
        ("Painter",  "514", 10),
        ("Taylor",   "3128", 70),
        ("Watson",   "100", 30),
        ("Watson",   "120", 50),
    ],
    "department": [
        ("Biology",     "Watson",   90000),
        ("Comp. Sci.",  "Taylor",   100000),
        ("Elec. Eng.",  "Taylor",   85000),
        ("Finance",     "Painter",  120000),
        ("History",     "Painter",  50000),
        ("Music",       "Packard",  80000),
        ("Physics",     "Watson",   70000),
    ],
    "course": [
        ("BIO-101", "Intro. to Biology",        "Biology",    4),
        ("BIO-301", "Genetics",                 "Biology",    4),
        ("BIO-399", "Computational Biology",    "Biology",    3),
        ("CS-101",  "Intro. to Computer Science","Comp. Sci.",4),
        ("CS-190",  "Game Design",              "Comp. Sci.", 4),
        ("CS-315",  "Robotics",                 "Comp. Sci.", 3),
        ("CS-319",  "Image Processing",         "Comp. Sci.", 3),
        ("CS-347",  "Database System Concepts", "Comp. Sci.", 3),
        ("EE-181",  "Intro. to Digital Systems","Elec. Eng.", 3),
        ("FIN-201", "Investment Banking",       "Finance",    3),
        ("HIS-351", "World History",            "History",    3),
        ("MU-199",  "Music Video Production",   "Music",      3),
        ("PHY-101", "Physical Principles",      "Physics",    4),
    ],
    "instructor": [
        ("10101", "Srinivasan", "Comp. Sci.", 65000),
        ("12121", "Wu",         "Finance",    90000),
        ("15151", "Mozart",     "Music",      40000),
        ("22222", "Einstein",   "Physics",    95000),
        ("32343", "El Said",    "History",    60000),
        ("33456", "Gold",       "Physics",    87000),
        ("45565", "Katz",       "Comp. Sci.", 75000),
        ("58583", "Califieri",  "History",    62000),
        ("76543", "Singh",      "Finance",    80000),
        ("76766", "Crick",      "Biology",    72000),
        ("83821", "Brandt",     "Comp. Sci.", 92000),
        ("98345", "Kim",        "Elec. Eng.", 80000),
    ],
    "time_slot": [
        ("A", "M", 8,  0,  8,  50),
        ("A", "W", 8,  0,  8,  50),
        ("A", "F", 8,  0,  8,  50),
        ("B", "M", 9,  0,  9,  50),
        ("B", "W", 9,  0,  9,  50),
        ("B", "F", 9,  0,  9,  50),
        ("C", "M", 11, 0,  11, 50),
        ("C", "W", 11, 0,  11, 50),
        ("C", "F", 11, 0,  11, 50),
        ("D", "M", 13, 0,  13, 50),
        ("D", "W", 13, 0,  13, 50),
        ("D", "F", 13, 0,  13, 50),
        ("E", "T", 10, 30, 11, 45),
        ("E", "R", 10, 30, 11, 45),
        ("F", "T", 14, 30, 15, 45),
        ("F", "R", 14, 30, 15, 45),
        ("G", "M", 16, 0,  16, 50),
        ("G", "W", 16, 0,  16, 50),
        ("G", "F", 16, 0,  16, 50),
        ("H", "W", 10, 0,  12, 30),
    ],
    "section": [
        ("BIO-101", "1", "Summer", 2017, "Painter",  "514",  "B"),
        ("BIO-301", "1", "Summer", 2018, "Painter",  "514",  "A"),
        ("CS-101",  "1", "Fall",   2017, "Packard",  "101",  "H"),
        ("CS-101",  "1", "Spring", 2018, "Packard",  "101",  "F"),
        ("CS-190",  "1", "Spring", 2017, "Taylor",   "3128", "E"),
        ("CS-190",  "2", "Spring", 2017, "Taylor",   "3128", "A"),
        ("CS-315",  "1", "Spring", 2018, "Watson",   "120",  "D"),
        ("CS-319",  "1", "Spring", 2018, "Watson",   "100",  "B"),
        ("CS-319",  "2", "Spring", 2018, "Taylor",   "3128", "C"),
        ("CS-347",  "1", "Fall",   2017, "Taylor",   "3128", "A"),
        ("EE-181",  "1", "Spring", 2017, "Taylor",   "3128", "C"),
        ("FIN-201", "1", "Spring", 2018, "Packard",  "101",  "B"),
        ("HIS-351", "1", "Spring", 2018, "Painter",  "514",  "C"),
        ("MU-199",  "1", "Spring", 2018, "Packard",  "101",  "D"),
        ("PHY-101", "1", "Fall",   2017, "Watson",   "100",  "A"),
    ],
    "teaches": [
        ("10101", "CS-101",  "1", "Fall",   2017),
        ("10101", "CS-315",  "1", "Spring", 2018),
        ("10101", "CS-347",  "1", "Fall",   2017),
        ("12121", "FIN-201", "1", "Spring", 2018),
        ("15151", "MU-199",  "1", "Spring", 2018),
        ("22222", "PHY-101", "1", "Fall",   2017),
        ("32343", "HIS-351", "1", "Spring", 2018),
        ("45565", "CS-101",  "1", "Spring", 2018),
        ("45565", "CS-319",  "1", "Spring", 2018),
        ("76766", "BIO-101", "1", "Summer", 2017),
        ("76766", "BIO-301", "1", "Summer", 2018),
        ("83821", "CS-190",  "1", "Spring", 2017),
        ("83821", "CS-190",  "2", "Spring", 2017),
        ("98345", "EE-181",  "1", "Spring", 2017),
    ],
    "student": [
        ("00128", "Zhang",      "Comp. Sci.", 102),
        ("12345", "Shankar",    "Comp. Sci.", 32),
        ("19991", "Brandt",     "History",    80),
        ("23121", "Chavez",     "Finance",    110),
        ("44553", "Peltier",    "Physics",    56),
        ("45678", "Levy",       "Physics",    46),
        ("54321", "Williams",   "Comp. Sci.", 54),
        ("55739", "Sanchez",    "Music",      38),
        ("70557", "Snow",       "Physics",    0),
        ("76543", "Brown",      "Comp. Sci.", 58),
        ("76653", "Aoi",        "Elec. Eng.", 60),
        ("98765", "Bouchard",   "Elec. Eng.", 98),
        ("98988", "Tanaka",     "Biology",    120),
    ],
    "takes": [
        ("00128", "CS-101",  "1", "Fall",   2017, "A"),
        ("00128", "CS-347",  "1", "Fall",   2017, "A-"),
        ("12345", "CS-101",  "1", "Fall",   2017, "C"),
        ("12345", "CS-190",  "2", "Spring", 2017, "A"),
        ("12345", "CS-315",  "1", "Spring", 2018, "A"),
        ("12345", "CS-347",  "1", "Fall",   2017, "A"),
        ("19991", "HIS-351", "1", "Spring", 2018, "B"),
        ("23121", "FIN-201", "1", "Spring", 2018, "C+"),
        ("44553", "PHY-101", "1", "Fall",   2017, "B-"),
        ("45678", "CS-101",  "1", "Fall",   2017, "F"),
        ("45678", "CS-101",  "1", "Spring", 2018, "B+"),
        ("45678", "CS-319",  "1", "Spring", 2018, "B"),
        ("54321", "CS-101",  "1", "Fall",   2017, "A-"),
        ("54321", "CS-190",  "2", "Spring", 2017, "B+"),
        ("55739", "MU-199",  "1", "Spring", 2018, "A"),
        ("76543", "CS-101",  "1", "Fall",   2017, "A"),
        ("76543", "CS-319",  "2", "Spring", 2018, "A"),
        ("76653", "EE-181",  "1", "Spring", 2017, "C"),
        ("98765", "CS-101",  "1", "Fall",   2017, "C-"),
        ("98765", "CS-315",  "1", "Spring", 2018, "B"),
        ("98988", "BIO-101", "1", "Summer", 2017, "A"),
        ("98988", "BIO-301", "1", "Summer", 2018, "A"),
    ],
    "advisor": [
        ("00128", "45565"),
        ("12345", "10101"),
        ("23121", "76543"),
        ("44553", "22222"),
        ("45678", "22222"),
        ("76543", "45565"),
        ("76653", "98345"),
        ("98765", "98345"),
        ("98988", "76766"),
    ],
    "prereq": [
        ("BIO-301", "BIO-101"),
        ("BIO-399", "BIO-101"),
        ("CS-190",  "CS-101"),
        ("CS-315",  "CS-101"),
        ("CS-319",  "CS-101"),
        ("CS-347",  "CS-101"),
        ("EE-181",  "PHY-101"),
    ],
}


def create_tables(conn: sqlite3.Connection) -> None:
    """建立所有資料表（若不存在）"""
    cursor = conn.cursor()
    # 啟用外鍵約束
    cursor.execute("PRAGMA foreign_keys = ON")
    for ddl in DDL_STATEMENTS:
        cursor.execute(ddl)
    conn.commit()
    print("✅ 資料表建立完成 (Tables created)")


def seed_data(conn: sqlite3.Connection) -> None:
    """將種子資料填入各資料表（按外鍵順序插入）"""
    cursor = conn.cursor()
    # 按照外鍵依賴順序插入資料
    table_order = [
        "classroom", "department", "course", "instructor",
        "time_slot", "section", "teaches", "student",
        "takes", "advisor", "prereq",
    ]
    for table in table_order:
        rows = SEED_DATA.get(table, [])
        if not rows:
            continue
        placeholders = ", ".join(["?"] * len(rows[0]))
        sql = f"INSERT OR IGNORE INTO {table} VALUES ({placeholders})"
        cursor.executemany(sql, rows)
        print(f"  📥 {table}: {cursor.rowcount} 筆資料已插入 (rows inserted)")
    conn.commit()
    print("✅ 種子資料填入完成 (Seed data loaded)")


def main() -> None:
    """主程式：若 DB 已存在則跳過，否則建立並填充"""
    if os.path.exists(DB_PATH):
        print(f"ℹ️  資料庫已存在，跳過建立: {DB_PATH}")
        return

    print(f"🔧 建立資料庫: {DB_PATH}")
    # 使用可讀寫模式建立資料庫（setup 期間需要寫入）
    conn = sqlite3.connect(DB_PATH)
    try:
        create_tables(conn)
        seed_data(conn)
        print(f"\n🎉 college_2.db 建立完成！路徑: {DB_PATH}")
    except Exception as e:
        print(f"❌ 建立失敗: {e}")
        conn.close()
        # 建立失敗時刪除不完整的資料庫
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
