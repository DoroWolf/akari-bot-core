import os
import shutil
import subprocess
import sys
import traceback
from queue import Queue, Empty
from threading import Thread
from time import sleep

import psutil
from loguru import logger

from config import Config
from database import BotDBUtil, session, DBVersion

encode = 'UTF-8'

bots_and_required_configs = {
    'aiocqhttp': [
        'qq_host',
        'qq_account'],
    'discord': ['discord_token'],
    'aiogram': ['telegram_token'],
    'kook': ['kook_token'],
    'matrix': [
        'matrix_homeserver',
        'matrix_user',
        'matrix_device_id',
        'matrix_token'],
    'api': []
}


class RestartBot(Exception):
    pass


def get_pid(name):
    return [p.pid for p in psutil.process_iter() if p.name().find(name) != -1]


def enqueue_output(out, queue):
    for line in iter(out.readline, b''):
        queue.put(line)
    out.close()


def init_bot():
    base_superuser = Config('base_superuser', cfg_type=(str, list))
    if base_superuser:
        if isinstance(base_superuser, str):
            base_superuser = [base_superuser]
        for bu in base_superuser:
            BotDBUtil.SenderInfo(bu).init()
            BotDBUtil.SenderInfo(bu).edit('isSuperUser', True)


pidlst = []

disabled_bots = Config('disabled_bots', [])


def run_bot():
    cache_path = os.path.abspath(Config('cache_path', './cache/'))
    if os.path.exists(cache_path):
        shutil.rmtree(cache_path)
    os.makedirs(cache_path, exist_ok=True)
    envs = os.environ.copy()
    envs['PYTHONIOENCODING'] = 'UTF-8'
    envs['PYTHONPATH'] = os.path.abspath('.')
    lst = bots_and_required_configs.keys()
    runlst = []
    for bl in lst:
        if bl in disabled_bots:
            continue
        if bl in bots_and_required_configs:
            abort = False
            for c in bots_and_required_configs[bl]:
                if not Config(c):
                    logger.error(f'Bot {bl} requires config {c} but not found, abort to launch.')
                    abort = True
                    break
            if abort:
                continue

        launch_args = [sys.executable, 'launcher.py', 'subprocess', bl]

        if sys.platform == 'win32' and 'launcher.exe' in os.listdir('.') and not sys.argv[0].endswith('.py'):
            launch_args = ['launcher.exe', 'subprocess', bl]

        elif 'launcher.bin' in os.listdir('.') and not sys.argv[0].endswith('.py'):
            launch_args = ['./launcher.bin', 'subprocess', bl]

        p = subprocess.Popen(launch_args, shell=False, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             cwd=os.path.abspath('.'), env=envs)
        runlst.append(p)
        pidlst.append(p.pid)

    q = Queue()
    threads = []
    for p in runlst:
        threads.append(Thread(target=enqueue_output, args=(p.stdout, q)))

    for t in threads:
        t.daemon = True
        t.start()

    while True:
        try:
            line = q.get_nowait()
        except Empty:
            sleep(1)
        else:
            try:
                logger.info(line.decode(encode)[:-1])
            except UnicodeDecodeError:
                encode_list = ['GBK']
                for e in encode_list:
                    try:
                        logger.warning(f'Cannot decode string from UTF-8, decode with {e}: '
                                       + line.decode(e)[:-1])
                        break
                    except Exception:
                        if encode_list[-1] != e:
                            logger.warning(f'Cannot decode string from {e}, '
                                           f'attempting with {encode_list[encode_list.index(e) + 1]}.')
                        else:
                            logger.error(f'Cannot decode string from {e}, no more attempts.')

        for p in runlst:
            if p.poll() == 233:
                logger.warning(f'{p.pid} exited with code 233, restart all bots.')
                pidlst.remove(p.pid)
                raise RestartBot

        # break when all processes are done.
        if all(p.poll() for p in runlst):
            break

        sleep(0.0001)


if __name__ == '__main__':
    logger.remove()
    logger.add(sys.stderr, format='{message}', level="INFO")
    query_dbver = session.query(DBVersion).first()
    if not query_dbver:
        session.add_all([DBVersion(value=str(BotDBUtil.database_version))])
        session.commit()
        query_dbver = session.query(DBVersion).first()
    if (current_ver := int(query_dbver.value)) < (target_ver := BotDBUtil.database_version):
        logger.info(f'Updating database from {current_ver} to {target_ver}...')
        from database.update import update_database

        update_database()
        logger.info('Database updated successfully!')
    init_bot()
    try:
        while True:
            try:
                run_bot()  # Process will block here so
                logger.critical('All bots exited unexpectedly, please check the output')
                break
            except RestartBot:
                for x in pidlst:
                    try:
                        os.kill(x, 9)
                    except (PermissionError, ProcessLookupError):
                        pass
                pidlst.clear()
                sleep(5)
                continue
            except Exception:
                logger.critical('An error occurred, please check the output.')
                traceback.print_exc()
                break
    except (KeyboardInterrupt, SystemExit):
        for x in pidlst:
            try:
                os.kill(x, 9)
            except (PermissionError, ProcessLookupError):
                pass
