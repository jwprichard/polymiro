# updown/ -- BTC momentum -> Polymarket binary-option trading pipeline
#
# Module dependency graph:
#   binance_ws -> signal -> executor
#   polymarket_ws -> loop
#   all -> types
#
# types.py is the leaf dependency; every other module in this package
# imports from it but it imports nothing from updown/.
