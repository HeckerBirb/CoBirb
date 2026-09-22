RECORDS = [
    {"name": "Ada", "email": "ADA@example.com"},
    {"name": "Bob"},
    {"name": "Cy", "email": "cy@Example.com"},
]


def load():
    return RECORDS


def summarise(records):
    return ", ".join(r["email"].lower() for r in records)


if __name__ == "__main__":
    print(summarise(load()))
