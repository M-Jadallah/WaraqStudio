"""Run management from the authorized Coolify container terminal only."""
import argparse
import time

from sqlalchemy import delete

from .config import Settings
from .db import Claim, Database, Enrollment, Phone, Ticket


def main():
    parser = argparse.ArgumentParser(description='Waraq gateway management')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init-db')
    enroll = sub.add_parser('enroll-code')
    enroll.add_argument('--minutes', type=int, default=10, choices=range(1, 61))
    sub.add_parser('prune')
    args = parser.parse_args()
    database = Database(Settings.from_env().database_url)
    if args.command == 'init-db':
        database.initialize()
        print('Gateway schema v1 is ready.')
    elif args.command == 'enroll-code':
        print(database.issue_enrollment(args.minutes))
        print(f'Single use; expires in {args.minutes} minutes. Do not share in logs.')
    elif args.command == 'prune':
        with database.session.begin() as session:
            for model in (Enrollment, Ticket, Claim):
                session.execute(delete(model).where(model.expires < time.time()))
        print('Expired one-time credentials removed.')


if __name__ == '__main__':
    main()
