import subprocess, sys
script = """
const { sumPrices } = require('./cart.js');
const a = sumPrices([{price: '10'}, {price: '20'}, {price: '30'}]);
const b = sumPrices([{price: 1.5}, {price: 2}]);
if (a !== 60 || b !== 3.5) { console.log(a, b); process.exit(1); }
"""
sys.exit(subprocess.run(["node", "-e", script]).returncode)
