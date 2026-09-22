import sys
import handlers
names = [n for n in dir(handlers) if n.startswith("handle_")]
for name in names:
    got = getattr(handlers, name)({"user": "u"})["code"]
    want = 204 if name == "handle_delete" else 200
    if got != want:
        print(name, got); sys.exit(1)
if handlers.handle_delete({})["code"] != 401:
    sys.exit(1)
