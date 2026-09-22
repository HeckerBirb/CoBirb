from dataclasses import dataclass, field


@dataclass
class Item:
    name: str
    price: float
    qty: int = 1


@dataclass
class Order:
    items: list[Item] = field(default_factory=list)
    tax_rate: float = 0.2
    discount_percent: float = 0
