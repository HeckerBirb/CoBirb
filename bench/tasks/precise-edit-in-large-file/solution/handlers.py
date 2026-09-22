"""Request handlers. Each returns a response dict."""


def handle_create(request):
    """Handle a create request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("create")
    return {"status": "ok", "code": 200}


def handle_read(request):
    """Handle a read request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("read")
    return {"status": "ok", "code": 200}


def handle_update(request):
    """Handle a update request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("update")
    return {"status": "ok", "code": 200}


def handle_list(request):
    """Handle a list request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("list")
    return {"status": "ok", "code": 200}


def handle_search(request):
    """Handle a search request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("search")
    return {"status": "ok", "code": 200}


def handle_export(request):
    """Handle a export request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("export")
    return {"status": "ok", "code": 200}


def handle_import(request):
    """Handle a import request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("import")
    return {"status": "ok", "code": 200}


def handle_archive(request):
    """Handle a archive request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("archive")
    return {"status": "ok", "code": 200}


def handle_restore(request):
    """Handle a restore request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("restore")
    return {"status": "ok", "code": 200}


def handle_publish(request):
    """Handle a publish request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("publish")
    return {"status": "ok", "code": 200}


def handle_unpublish(request):
    """Handle a unpublish request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("unpublish")
    return {"status": "ok", "code": 200}


def handle_tag(request):
    """Handle a tag request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("tag")
    return {"status": "ok", "code": 200}


def handle_untag(request):
    """Handle a untag request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("untag")
    return {"status": "ok", "code": 200}


def handle_share(request):
    """Handle a share request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("share")
    return {"status": "ok", "code": 200}


def handle_unshare(request):
    """Handle a unshare request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("unshare")
    return {"status": "ok", "code": 200}


def handle_lock(request):
    """Handle a lock request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("lock")
    return {"status": "ok", "code": 200}


def handle_unlock(request):
    """Handle a unlock request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("unlock")
    return {"status": "ok", "code": 200}


def handle_copy(request):
    """Handle a copy request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("copy")
    return {"status": "ok", "code": 200}


def handle_move(request):
    """Handle a move request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("move")
    return {"status": "ok", "code": 200}


def handle_delete(request):
    """Handle a delete request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("delete")
    return {"status": "ok", "code": 204}


def handle_purge(request):
    """Handle a purge request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("purge")
    return {"status": "ok", "code": 200}


def handle_audit(request):
    """Handle a audit request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("audit")
    return {"status": "ok", "code": 200}


def handle_notify(request):
    """Handle a notify request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("notify")
    return {"status": "ok", "code": 200}


def handle_subscribe(request):
    """Handle a subscribe request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("subscribe")
    return {"status": "ok", "code": 200}


def handle_unsubscribe(request):
    """Handle a unsubscribe request."""
    user = request.get("user")
    if user is None:
        return {"status": "error", "code": 401}
    request.setdefault("log", []).append("unsubscribe")
    return {"status": "ok", "code": 200}

