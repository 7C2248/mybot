import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from server.config import ServiceSettings, PROJECT_ROOT
from server.services.files import atomic_write


class APIError(RuntimeError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def local_url(value):
    url = urlsplit(value)
    if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost') or url.username
            or url.password or url.query or url.fragment or url.path not in ('', '/')):
        raise argparse.ArgumentTypeError('服务地址必须是本机 HTTP 地址')
    return f'http://127.0.0.1:{url.port or 80}'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='通过本机 API 使用与桌面共享的会话、历史和记忆策略。')
    parser.add_argument('--character-name', default='SuLi')
    parser.add_argument('--thread-id', default='3592f14a-f2e3-44ff-9d40-f6380313cfa6')
    parser.add_argument('--new-thread', action='store_true', help='创建新的独立会话')
    parser.add_argument('--title', default='CLI 对话')
    parser.add_argument('--api-url', type=local_url, default=f'http://127.0.0.1:{ServiceSettings.from_environment().port}')
    parser.add_argument('--memory-retrieval', choices=('on', 'off'), help='检索长期记忆；省略时保留已有会话策略，新会话默认关闭')
    parser.add_argument('--memory-storage', choices=('on', 'off'), help='存储长期记忆；省略时保留已有会话策略，新会话默认关闭')
    parser.add_argument('--configure-only', action='store_true', help='读取/修改会话策略后退出')
    return parser.parse_args(argv)


class CLIClient:
    def __init__(self, base_url, *, transport=None, cache_root=None):
        self.http = httpx.AsyncClient(base_url=local_url(base_url) + '/api/',
            headers={'X-Mybot-Client': 'mybot-desktop'}, timeout=15, transport=transport, trust_env=False)
        self.cache_root = cache_root or PROJECT_ROOT / 'data' / 'cli'

    async def request(self, method, path, body=None):
        try:
            response = await self.http.request(method, path, json=body) if body is not None else await self.http.request(method, path)
        except httpx.HTTPError:
            raise APIError('无法连接本机 API。请使用 start_cli.py 启动服务，或检查 --api-url。') from None
        if not response.is_success:
            try:
                message = response.json()['error']['message']
            except (ValueError, KeyError, TypeError):
                message = f'服务请求失败（{response.status_code}）'
            raise APIError(message, response.status_code)
        return response.json()

    async def resolve(self, args):
        source = str(uuid4()) if args.new_thread else args.thread_id
        job = await self.request('POST', 'cli/resolve', {
            'source_id': source, 'character_id': args.character_name, 'title': args.title,
            'memory_retrieval_enabled': args.memory_retrieval == 'on',
            'memory_storage_enabled': args.memory_storage == 'on', 'source': 'cli'})
        from urllib.parse import quote
        if job['status'] in ('queued', 'running'):
            print('正在导入旧 CLI 历史，请稍候。', flush=True)
        while job['status'] in ('queued', 'running'):
            await asyncio.sleep(0.5)
            job = await self.request('GET', f"legacy/threads/{quote(source, safe='')}")
        if job['status'] != 'completed':
            raise APIError(f"旧 CLI 历史未完成导入（{job.get('error_code') or job['status']}），请在桌面会话管理中查看并重试。")
        thread = await self.request('GET', f"threads/{job['thread_id']}")
        changes = {}
        for option, field in (('memory_retrieval', 'memory_retrieval_enabled'), ('memory_storage', 'memory_storage_enabled')):
            if getattr(args, option) is not None:
                changes[field] = getattr(args, option) == 'on'
        if changes and any(thread[k] != v for k, v in changes.items()):
            thread = await self.update_policy(thread['id'], changes)
        return thread

    async def update_policy(self, thread_id, changes):
        thread = await self.request('GET', f'threads/{thread_id}')
        return await self.request('PATCH', f'threads/{thread_id}', {'expected_version': thread['version'], **changes})

    async def memory_command(self, thread_id, command):
        parts = command.split()
        if parts == ['/memory']:
            thread = await self.request('GET', f'threads/{thread_id}')
        elif len(parts) == 3 and parts[1] in ('retrieval', 'storage') and parts[2] in ('on', 'off'):
            thread = await self.update_policy(thread_id, {f'memory_{parts[1]}_enabled': parts[2] == 'on'})
        elif len(parts) == 2 and parts[1] in ('on', 'off'):
            thread = await self.update_policy(thread_id, {'memory_retrieval_enabled': parts[1] == 'on', 'memory_storage_enabled': parts[1] == 'on'})
        else:
            raise APIError('用法：/memory，/memory retrieval on|off，/memory storage on|off，/memory on|off')
        self.show_policy(thread)
        return thread

    @staticmethod
    def show_policy(thread):
        print(f"会话：{thread['id']} · {thread['character_id']} · {thread['title']}")
        print(f"检索记忆：{'开启' if thread['memory_retrieval_enabled'] else '关闭'}；存储记忆：{'开启' if thread['memory_storage_enabled'] else '关闭'}")

    def pending_path(self, thread_id):
        key = hashlib.sha256((str(self.http.base_url) + str(thread_id)).encode()).hexdigest()
        return self.cache_root / f'{key}.json'

    async def run(self, thread_id, text=None, *, retry_of=None):
        path = self.pending_path(thread_id)
        if path.exists():
            pending = json.loads(path.read_text(encoding='utf-8'))
        else:
            pending = {'client_request_id': str(uuid4()), 'text': text, 'retry_of': retry_of}
            self.cache_root.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps(pending, ensure_ascii=False))
        body = {'client_request_id': pending['client_request_id']}
        if pending['retry_of']:
            endpoint = f"runs/{pending['retry_of']}/retry"
        else:
            endpoint = f'threads/{thread_id}/runs'
            body['text'] = pending['text']
        try:
            accepted = await self.request('POST', endpoint, body)
        except APIError as error:
            if 400 <= error.status < 500:
                if pending.get('text'):
                    print('未发送的原文：', pending['text'])
                path.unlink(missing_ok=True)
            raise
        while True:
            snapshot = await self.request('GET', f"runs/{accepted['run_id']}")
            if snapshot['status'] not in ('queued', 'running'):
                path.unlink(missing_ok=True)
                return snapshot
            await asyncio.sleep(0.5)


async def chat(args):
    client = CLIClient(args.api_url)
    try:
        thread = await client.resolve(args)
        client.show_policy(thread)
        if args.configure_only:
            return
        page = await client.request('GET', f"threads/{thread['id']}/messages")
        if thread.get('history_notice'):
            print(thread['history_notice'])
        for message in page['items']:
            print(f"{'User' if message['role'] == 'user' else thread['character_id']}: {message['text']}")
        if page['next_cursor']:
            print('更早的完整历史可在桌面中查看。')
        print('命令：/memory 查看策略；/memory retrieval on|off；/memory storage on|off；/retry 重试失败运行；exit 退出。')
        last_run = None
        while True:
            try:
                if client.pending_path(thread['id']).exists():
                    print('恢复上次尚未确认的请求，使用原请求标识。')
                    snapshot = await client.run(thread['id'])
                else:
                    text = await asyncio.to_thread(input, 'User: ')
                    if text.lower() == 'exit':
                        break
                    if text.startswith('/memory'):
                        thread = await client.memory_command(thread['id'], text)
                        continue
                    if not text.strip():
                        continue
                    if text == '/retry':
                        # GET list summaries also lets a restarted CLI discover a failed run.
                        if last_run is None:
                            current = await client.request('GET', f"threads/{thread['id']}")
                            last_run = current.get('latest_run')
                        if not last_run or last_run['status'] not in ('failed', 'interrupted'):
                            raise APIError('当前没有可重试的失败运行。')
                        snapshot = await client.run(thread['id'], retry_of=last_run['id'])
                    else:
                        snapshot = await client.run(thread['id'], text.replace('\\n', '\n'))
                last_run = snapshot
                for message in snapshot['messages']:
                    if message['role'] == 'assistant':
                        print(f"{thread['character_id']}: {message['text']}")
                if snapshot['status'] in ('failed', 'interrupted'):
                    print('本轮未提交正式回复，可以使用 /retry 明确重试。')
                elif snapshot['status'] == 'completed_with_warnings':
                    print('回复已保存，后续处理存在异常。')
            except APIError as error:
                print(str(error))
                if client.pending_path(thread['id']).exists():
                    print('请求标识和原文已保留，下次启动继续确认。')
                    break
    finally:
        await client.http.aclose()


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(chat(args))
    except (KeyboardInterrupt, EOFError):
        pass
    except (APIError, ValueError) as error:
        print(str(error))
        return 1
    return 0
