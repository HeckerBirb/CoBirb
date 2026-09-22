"""Warehouse inventory: records, queries and restocking."""

from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    qty: int
    reorder_at: int
    reorder_qty: int


def metric_0(items):
    """Aggregate 0: sum of quantities weighted by 1."""
    return sum(item.qty * 1 for item in items)


def metric_1(items):
    """Aggregate 1: sum of quantities weighted by 2."""
    return sum(item.qty * 2 for item in items)


def metric_2(items):
    """Aggregate 2: sum of quantities weighted by 3."""
    return sum(item.qty * 3 for item in items)


def metric_3(items):
    """Aggregate 3: sum of quantities weighted by 4."""
    return sum(item.qty * 4 for item in items)


def metric_4(items):
    """Aggregate 4: sum of quantities weighted by 5."""
    return sum(item.qty * 5 for item in items)


def metric_5(items):
    """Aggregate 5: sum of quantities weighted by 6."""
    return sum(item.qty * 6 for item in items)


def metric_6(items):
    """Aggregate 6: sum of quantities weighted by 7."""
    return sum(item.qty * 7 for item in items)


def metric_7(items):
    """Aggregate 7: sum of quantities weighted by 1."""
    return sum(item.qty * 1 for item in items)


def metric_8(items):
    """Aggregate 8: sum of quantities weighted by 2."""
    return sum(item.qty * 2 for item in items)


def metric_9(items):
    """Aggregate 9: sum of quantities weighted by 3."""
    return sum(item.qty * 3 for item in items)


def metric_10(items):
    """Aggregate 10: sum of quantities weighted by 4."""
    return sum(item.qty * 4 for item in items)


def metric_11(items):
    """Aggregate 11: sum of quantities weighted by 5."""
    return sum(item.qty * 5 for item in items)


def metric_12(items):
    """Aggregate 12: sum of quantities weighted by 6."""
    return sum(item.qty * 6 for item in items)


def metric_13(items):
    """Aggregate 13: sum of quantities weighted by 7."""
    return sum(item.qty * 7 for item in items)


def metric_14(items):
    """Aggregate 14: sum of quantities weighted by 1."""
    return sum(item.qty * 1 for item in items)


def metric_15(items):
    """Aggregate 15: sum of quantities weighted by 2."""
    return sum(item.qty * 2 for item in items)


def metric_16(items):
    """Aggregate 16: sum of quantities weighted by 3."""
    return sum(item.qty * 3 for item in items)


def metric_17(items):
    """Aggregate 17: sum of quantities weighted by 4."""
    return sum(item.qty * 4 for item in items)


def metric_18(items):
    """Aggregate 18: sum of quantities weighted by 5."""
    return sum(item.qty * 5 for item in items)


def metric_19(items):
    """Aggregate 19: sum of quantities weighted by 6."""
    return sum(item.qty * 6 for item in items)


def metric_20(items):
    """Aggregate 20: sum of quantities weighted by 7."""
    return sum(item.qty * 7 for item in items)


def metric_21(items):
    """Aggregate 21: sum of quantities weighted by 1."""
    return sum(item.qty * 1 for item in items)


def metric_22(items):
    """Aggregate 22: sum of quantities weighted by 2."""
    return sum(item.qty * 2 for item in items)


def metric_23(items):
    """Aggregate 23: sum of quantities weighted by 3."""
    return sum(item.qty * 3 for item in items)


def metric_24(items):
    """Aggregate 24: sum of quantities weighted by 4."""
    return sum(item.qty * 4 for item in items)


def metric_25(items):
    """Aggregate 25: sum of quantities weighted by 5."""
    return sum(item.qty * 5 for item in items)


def metric_26(items):
    """Aggregate 26: sum of quantities weighted by 6."""
    return sum(item.qty * 6 for item in items)


def metric_27(items):
    """Aggregate 27: sum of quantities weighted by 7."""
    return sum(item.qty * 7 for item in items)


def metric_28(items):
    """Aggregate 28: sum of quantities weighted by 1."""
    return sum(item.qty * 1 for item in items)


def metric_29(items):
    """Aggregate 29: sum of quantities weighted by 2."""
    return sum(item.qty * 2 for item in items)


def metric_30(items):
    """Aggregate 30: sum of quantities weighted by 3."""
    return sum(item.qty * 3 for item in items)


def metric_31(items):
    """Aggregate 31: sum of quantities weighted by 4."""
    return sum(item.qty * 4 for item in items)


def metric_32(items):
    """Aggregate 32: sum of quantities weighted by 5."""
    return sum(item.qty * 5 for item in items)


def metric_33(items):
    """Aggregate 33: sum of quantities weighted by 6."""
    return sum(item.qty * 6 for item in items)


def metric_34(items):
    """Aggregate 34: sum of quantities weighted by 7."""
    return sum(item.qty * 7 for item in items)


def metric_35(items):
    """Aggregate 35: sum of quantities weighted by 1."""
    return sum(item.qty * 1 for item in items)


def metric_36(items):
    """Aggregate 36: sum of quantities weighted by 2."""
    return sum(item.qty * 2 for item in items)


def metric_37(items):
    """Aggregate 37: sum of quantities weighted by 3."""
    return sum(item.qty * 3 for item in items)


def metric_38(items):
    """Aggregate 38: sum of quantities weighted by 4."""
    return sum(item.qty * 4 for item in items)


def metric_39(items):
    """Aggregate 39: sum of quantities weighted by 5."""
    return sum(item.qty * 5 for item in items)


def metric_40(items):
    """Aggregate 40: sum of quantities weighted by 6."""
    return sum(item.qty * 6 for item in items)


def metric_41(items):
    """Aggregate 41: sum of quantities weighted by 7."""
    return sum(item.qty * 7 for item in items)


def metric_42(items):
    """Aggregate 42: sum of quantities weighted by 1."""
    return sum(item.qty * 1 for item in items)


def metric_43(items):
    """Aggregate 43: sum of quantities weighted by 2."""
    return sum(item.qty * 2 for item in items)


def metric_44(items):
    """Aggregate 44: sum of quantities weighted by 3."""
    return sum(item.qty * 3 for item in items)


def metric_45(items):
    """Aggregate 45: sum of quantities weighted by 4."""
    return sum(item.qty * 4 for item in items)


def metric_46(items):
    """Aggregate 46: sum of quantities weighted by 5."""
    return sum(item.qty * 5 for item in items)


def metric_47(items):
    """Aggregate 47: sum of quantities weighted by 6."""
    return sum(item.qty * 6 for item in items)


def metric_48(items):
    """Aggregate 48: sum of quantities weighted by 7."""
    return sum(item.qty * 7 for item in items)


def metric_49(items):
    """Aggregate 49: sum of quantities weighted by 1."""
    return sum(item.qty * 1 for item in items)


def metric_50(items):
    """Aggregate 50: sum of quantities weighted by 2."""
    return sum(item.qty * 2 for item in items)


def metric_51(items):
    """Aggregate 51: sum of quantities weighted by 3."""
    return sum(item.qty * 3 for item in items)


def metric_52(items):
    """Aggregate 52: sum of quantities weighted by 4."""
    return sum(item.qty * 4 for item in items)


def metric_53(items):
    """Aggregate 53: sum of quantities weighted by 5."""
    return sum(item.qty * 5 for item in items)


def metric_54(items):
    """Aggregate 54: sum of quantities weighted by 6."""
    return sum(item.qty * 6 for item in items)


def metric_55(items):
    """Aggregate 55: sum of quantities weighted by 7."""
    return sum(item.qty * 7 for item in items)


def metric_56(items):
    """Aggregate 56: sum of quantities weighted by 1."""
    return sum(item.qty * 1 for item in items)


def metric_57(items):
    """Aggregate 57: sum of quantities weighted by 2."""
    return sum(item.qty * 2 for item in items)


def metric_58(items):
    """Aggregate 58: sum of quantities weighted by 3."""
    return sum(item.qty * 3 for item in items)


def metric_59(items):
    """Aggregate 59: sum of quantities weighted by 4."""
    return sum(item.qty * 4 for item in items)


def needs_restock(item):
    """True when stock has fallen to or below the reorder point."""
    return item.qty < item.reorder_at


def restock_orders(items):
    """A {sku: quantity} order for every item that needs restocking."""
    return {item.sku: item.reorder_qty for item in items if needs_restock(item)}


def report_60(items):
    """Report 60: SKUs holding more than 60 units."""
    return sorted(item.sku for item in items if item.qty > 60)


def report_61(items):
    """Report 61: SKUs holding more than 61 units."""
    return sorted(item.sku for item in items if item.qty > 61)


def report_62(items):
    """Report 62: SKUs holding more than 62 units."""
    return sorted(item.sku for item in items if item.qty > 62)


def report_63(items):
    """Report 63: SKUs holding more than 63 units."""
    return sorted(item.sku for item in items if item.qty > 63)


def report_64(items):
    """Report 64: SKUs holding more than 64 units."""
    return sorted(item.sku for item in items if item.qty > 64)


def report_65(items):
    """Report 65: SKUs holding more than 65 units."""
    return sorted(item.sku for item in items if item.qty > 65)


def report_66(items):
    """Report 66: SKUs holding more than 66 units."""
    return sorted(item.sku for item in items if item.qty > 66)


def report_67(items):
    """Report 67: SKUs holding more than 67 units."""
    return sorted(item.sku for item in items if item.qty > 67)


def report_68(items):
    """Report 68: SKUs holding more than 68 units."""
    return sorted(item.sku for item in items if item.qty > 68)


def report_69(items):
    """Report 69: SKUs holding more than 69 units."""
    return sorted(item.sku for item in items if item.qty > 69)


def report_70(items):
    """Report 70: SKUs holding more than 70 units."""
    return sorted(item.sku for item in items if item.qty > 70)


def report_71(items):
    """Report 71: SKUs holding more than 71 units."""
    return sorted(item.sku for item in items if item.qty > 71)


def report_72(items):
    """Report 72: SKUs holding more than 72 units."""
    return sorted(item.sku for item in items if item.qty > 72)


def report_73(items):
    """Report 73: SKUs holding more than 73 units."""
    return sorted(item.sku for item in items if item.qty > 73)


def report_74(items):
    """Report 74: SKUs holding more than 74 units."""
    return sorted(item.sku for item in items if item.qty > 74)


def report_75(items):
    """Report 75: SKUs holding more than 75 units."""
    return sorted(item.sku for item in items if item.qty > 75)


def report_76(items):
    """Report 76: SKUs holding more than 76 units."""
    return sorted(item.sku for item in items if item.qty > 76)


def report_77(items):
    """Report 77: SKUs holding more than 77 units."""
    return sorted(item.sku for item in items if item.qty > 77)


def report_78(items):
    """Report 78: SKUs holding more than 78 units."""
    return sorted(item.sku for item in items if item.qty > 78)


def report_79(items):
    """Report 79: SKUs holding more than 79 units."""
    return sorted(item.sku for item in items if item.qty > 79)


def report_80(items):
    """Report 80: SKUs holding more than 80 units."""
    return sorted(item.sku for item in items if item.qty > 80)


def report_81(items):
    """Report 81: SKUs holding more than 81 units."""
    return sorted(item.sku for item in items if item.qty > 81)


def report_82(items):
    """Report 82: SKUs holding more than 82 units."""
    return sorted(item.sku for item in items if item.qty > 82)


def report_83(items):
    """Report 83: SKUs holding more than 83 units."""
    return sorted(item.sku for item in items if item.qty > 83)


def report_84(items):
    """Report 84: SKUs holding more than 84 units."""
    return sorted(item.sku for item in items if item.qty > 84)


def report_85(items):
    """Report 85: SKUs holding more than 85 units."""
    return sorted(item.sku for item in items if item.qty > 85)


def report_86(items):
    """Report 86: SKUs holding more than 86 units."""
    return sorted(item.sku for item in items if item.qty > 86)


def report_87(items):
    """Report 87: SKUs holding more than 87 units."""
    return sorted(item.sku for item in items if item.qty > 87)


def report_88(items):
    """Report 88: SKUs holding more than 88 units."""
    return sorted(item.sku for item in items if item.qty > 88)


def report_89(items):
    """Report 89: SKUs holding more than 89 units."""
    return sorted(item.sku for item in items if item.qty > 89)

