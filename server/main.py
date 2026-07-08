import uvicorn

from server.api import app


def run() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8787)


if __name__ == "__main__":
    run()
