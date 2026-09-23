"""One local process, without Uvicorn reload or worker supervision."""

import argparse
import asyncio
import sys

from server.config import ServiceSettings


def create_event_loop():
    # Run Server.serve ourselves: Uvicorn's Windows loop factory may choose Proactor.
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


def main():
    parser = argparse.ArgumentParser(description="启动 mybot 本机 API 服务（仅监听 127.0.0.1）")
    parser.add_argument("--port", type=int, help="监听端口，默认 8765 或 MYBOT_API_PORT")
    args = parser.parse_args()
    try:
        from dataclasses import replace
        settings = ServiceSettings.from_environment()
        if args.port is not None:
            settings = replace(settings, port=args.port)
    except ValueError:
        parser.error("服务配置无效，请检查端口、超时和记忆 schema 配置。")

    import uvicorn
    from utils.daily_logger import configure_backend_logging
    configure_backend_logging()
    from server.app import create_app

    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=settings.port,
                            workers=1, reload=False, proxy_headers=False, server_header=False,
                            timeout_graceful_shutdown=10, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    config.app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    try:
        with asyncio.Runner(loop_factory=create_event_loop) as runner:
            runner.run(server.serve())
    except KeyboardInterrupt:
        pass
    if not server.started:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
