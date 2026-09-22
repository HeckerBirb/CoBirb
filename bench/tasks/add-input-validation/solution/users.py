def create_user(name, age):
    if not name or not name.strip():
        raise ValueError("name must not be empty")
    if age < 0:
        raise ValueError("age must not be negative")
    return {"name": name.strip(), "age": age}
