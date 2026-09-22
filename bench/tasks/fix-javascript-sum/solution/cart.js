function sumPrices(items) {
  return items.reduce((total, item) => total + Number(item.price), 0);
}

module.exports = { sumPrices };
