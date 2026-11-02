"""Add up a receipt: one item a line, its name then its price, as "coffee 3.50"."""
import sys


def total(text):
    """The receipt's total in cents; blank lines and lines starting with # are skipped."""
    cents = 0
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, price = line.rsplit(None, 1)
            cents += round(float(price) * 100)
    return cents


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 receipt.py RECEIPT.txt", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        cents = total(f.read())
    print(f"Total: ${cents / 100:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
