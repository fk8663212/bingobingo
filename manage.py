import argparse
import getpass
import json
import os
from pathlib import Path

from bingo.game import Problem, Store


def main():
    parser = argparse.ArgumentParser(description='Bingo setup and backup')
    parser.add_argument('--db', default=os.environ.get('BINGO_DB', 'data/bingo.sqlite3'))
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='初始化正式活動，互動設定密碼')
    init.add_argument('--title', default='導生晚宴')
    init.add_argument('--tickets', type=int, default=120)
    sub.add_parser('password', help='互動修改主持人密碼並使舊登入失效')
    sub.add_parser('demo', help='建立僅供本機的獨立示範資料庫')
    backup = sub.add_parser('backup', help='使用 SQLite online backup 安全備份')
    backup.add_argument('destination')
    export = sub.add_parser('export', help='匯出完整稽核紀錄（含秘密領卡碼）')
    export.add_argument('destination')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'demo':
            path = Path('data/demo.sqlite3')
            if path.exists():
                raise Problem('示範資料已存在；可直接啟動，不會覆寫。')
            store = Store(path)
            store.initialize('demo-host-2026!', '導生晚宴・本機示範', demo=True)
            store.create_tickets(150)
            store.registration(True, 1)
            print('示範資料：data/demo.sqlite3\n主持人密碼：demo-host-2026!\n啟動：python3 app.py --db data/demo.sqlite3')
            return
        store = Store(args.db)
        if args.command in ('init', 'password'):
            password = getpass.getpass('主持人密碼（至少 12 字元）：')
            if password != getpass.getpass('再次輸入密碼：'):
                raise Problem('兩次密碼不相同。')
            if args.command == 'init':
                if not 1 <= args.tickets <= 200:
                    raise Problem('初始領卡碼數量須介於 1～200。')
                store.initialize(password, args.title)
                store.create_tickets(args.tickets)
            else:
                store.change_password(password)
            print('設定完成。密碼不會儲存在指令紀錄。')
        elif args.command == 'backup':
            store.backup(args.destination)
            print('備份完成：' + args.destination)
        elif args.command == 'export':
            Path(args.destination).write_text(json.dumps(store.export_all(), ensure_ascii=False, indent=2), encoding='utf-8')
            print('匯出完成：' + args.destination)
    except Problem as e:
        parser.exit(1, str(e) + '\n')


if __name__ == '__main__':
    main()
