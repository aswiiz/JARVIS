"""Storage: MongoDB (chat history + generated files via GridFS) with an in-memory fallback."""
import logging
import os
import secrets
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone

log = logging.getLogger("jarvis.db")


class MemoryStore:
    kind = "memory"

    def __init__(self):
        self.messages = defaultdict(list)
        self.files = OrderedDict()

    def add_message(self, session_id, role, content, meta=None):
        self.messages[session_id].append(
            {"role": role, "content": content, "meta": meta or {}, "ts": time.time()}
        )
        self.messages[session_id] = self.messages[session_id][-100:]

    def history(self, session_id, limit=10):
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self.messages[session_id][-limit:]
        ]

    def save_file(self, data, mime, name=None):
        token = secrets.token_urlsafe(24)
        self.files[token] = (data, mime)
        while len(self.files) > 30:
            self.files.popitem(last=False)
        return token

    def get_file(self, token):
        return self.files.get(token)

    def cleanup(self, days):
        pass


class MongoStore:
    kind = "mongodb"

    def __init__(self, uri, dbname):
        from pymongo import MongoClient
        import gridfs

        self.client = MongoClient(uri, serverSelectionTimeoutMS=8000)
        self.client.admin.command("ping")
        self.db = self.client[dbname]
        self.fs = gridfs.GridFS(self.db)
        self.col = self.db["messages"]
        self.col.create_index([("session_id", 1), ("ts", 1)])

    def add_message(self, session_id, role, content, meta=None):
        self.col.insert_one(
            {"session_id": session_id, "role": role, "content": content,
             "meta": meta or {}, "ts": time.time()}
        )

    def history(self, session_id, limit=10):
        cur = self.col.find({"session_id": session_id}).sort("ts", -1).limit(limit)
        items = list(cur)[::-1]
        return [{"role": m["role"], "content": m["content"]} for m in items]

    def save_file(self, data, mime, name=None):
        token = secrets.token_urlsafe(24)
        self.fs.put(data, filename=name or token, content_type=mime, token=token)
        return token

    def get_file(self, token):
        f = self.fs.find_one({"token": token})
        if not f:
            return None
        return f.read(), (f.content_type or "application/octet-stream")

    def cleanup(self, days):
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        removed = 0
        for f in self.fs.find({"uploadDate": {"$lt": cutoff}}):
            self.fs.delete(f._id)
            removed += 1
        if removed:
            log.info("Removed %d old generated files", removed)


def make_store():
    uri = os.getenv("MONGODB_URI", "").strip()
    if uri:
        try:
            store = MongoStore(uri, os.getenv("MONGODB_DB", "jarvis"))
            log.info("Connected to MongoDB")
            return store
        except Exception as exc:
            log.error("MongoDB connection failed (%s). Falling back to memory.", exc)
    else:
        log.warning("MONGODB_URI not set. Using in-memory storage (lost on restart).")
    return MemoryStore()
