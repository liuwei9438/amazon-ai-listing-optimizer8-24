# -*- coding: utf-8 -*-
"""本地生成密码哈希——用于 Streamlit Secrets 的账号门配置。

用法（在本文件目录打开命令行）：
    python make_password_hash.py

按提示输入密码（输入时不显示），把打印出来的一行
    账号名 = "pbkdf2$260000$...$..."
粘到 Streamlit Cloud 的 Secrets 里，例如：

    [app_users]
    zhangsan = "pbkdf2$260000$...$..."

明文密码不会被保存、不会出现在屏幕上，只输出哈希。
"""
import getpass
import hashlib
import os

ITERATIONS = 260_000


def make_hash(password: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS
    ).hexdigest()
    return f"pbkdf2${ITERATIONS}${salt}${digest}"


def main() -> None:
    print("=== 账号密码哈希生成器（本地使用，不上传） ===")
    name = input("账号名（英文小写，比如 zhangsan）：").strip().lower()
    if not name:
        raise SystemExit("账号名不能为空")
    pwd = getpass.getpass("为该账号设置的密码：")
    pwd2 = getpass.getpass("再输入一次确认：")
    if not pwd or pwd != pwd2:
        raise SystemExit("两次输入不一致或密码为空，请重跑")
    print()
    print("把下面这一行加进 Streamlit Secrets 的 [app_users] 段：")
    print(f'{name} = "{make_hash(pwd)}"')


if __name__ == "__main__":
    main()
