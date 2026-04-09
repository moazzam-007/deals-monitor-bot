"""
Generate a Pyrogram session string for the userbot.
Run this script locally ONCE — paste the output into your
STRING_SESSION environment variable.

Usage:
    pip install pyrogram tgcrypto
    python generate_session.py
"""

from pyrogram import Client

API_ID = int(input("Enter your API_ID: "))
API_HASH = input("Enter your API_HASH: ")

with Client(name=":memory:", api_id=API_ID, api_hash=API_HASH, in_memory=True) as app:
    session_string = app.export_session_string()
    print("\n✅ Your session string (copy the entire line below):\n")
    print(session_string)
    print("\nStore this in your STRING_SESSION environment variable.")
