#!/usr/bin/env python3
import io
import json
import re
import sys
import os
import glob

import click

import coin_info

try:
    import mako
    import mako.template
    from munch import Munch

    CAN_RENDER = True
except ImportError:
    CAN_RENDER = False

try:
    import requests
except ImportError:
    requests = None

try:
    import binascii
    import struct
    import zlib
    from hashlib import sha256
    import ed25519
    from PIL import Image
    from trezorlib import protobuf
    from coindef import CoinDef

    CAN_BUILD_DEFS = True
except ImportError:
    CAN_BUILD_DEFS = False


# ======= Mako management ======


def c_str_filter(b):
    if b is None:
        return "NULL"

    def hexescape(c):
        return r"\x{:02x}".format(c)

    if isinstance(b, bytes):
        return '"' + "".join(map(hexescape, b)) + '"'
    else:
        return json.dumps(b)


def ascii_filter(s):
    return re.sub("[^ -\x7e]", "_", s)


MAKO_FILTERS = {"c_str": c_str_filter, "ascii": ascii_filter}


def render_file(filename, coins, support_info):
    """Opens `filename.j2`, renders the template and stores the result in `filename`."""
    template = mako.template.Template(filename=filename + ".mako")
    result = template.render(support_info=support_info, **coins, **MAKO_FILTERS)
    with open(filename, "w") as f:
        f.write(result)


# ====== validation functions ======


def check_support(defs, support_data, fail_missing=False):
    check_passed = True
    coin_list = defs.as_list()
    coin_names = {coin["key"]: coin["name"] for coin in coin_list}

    def coin_name(key):
        if key in coin_names:
            return "{} ({})".format(key, coin_names[key])
        else:
            return "{} <unknown key>".format(key)

    for key, support in support_data.items():
        errors = coin_info.validate_support(support)
        if errors:
            check_passed = False
            print("ERR:", "invalid definition for", coin_name(key))
            print("\n".join(errors))

    expected_coins = set(coin["key"] for coin in defs.coins + defs.misc)

    # detect missing support info for expected
    for coin in expected_coins:
        if coin not in support_data:
            if fail_missing:
                check_passed = False
                print("ERR: Missing support info for", coin_name(coin))
            else:
                print("WARN: Missing support info for", coin_name(coin))

    # detect non-matching support info
    coin_set = set(coin["key"] for coin in coin_list)
    for key in support_data:
        # detect non-matching support info
        if key not in coin_set:
            check_passed = False
            print("ERR: Support info found for unknown coin", key)

        # detect override - doesn't fail check
        if key not in expected_coins:
            print("INFO: Override present for coin", coin_name(key))

    return check_passed


def check_btc(coins):
    check_passed = True

    for coin in coins:
        errors = coin_info.validate_btc(coin)
        if errors:
            check_passed = False
            print("ERR:", "invalid definition for", coin["name"])
            print("\n".join(errors))

    collisions = coin_info.find_address_collisions(coins)
    # warning only
    for key, dups in collisions.items():
        if dups:
            print("WARN: collisions found in", key)
            for k, v in dups.items():
                print("-", k, ":", ", ".join(map(str, v)))

    return check_passed


def check_backends(coins):
    check_passed = True
    for coin in coins:
        genesis_block = coin.get("hash_genesis_block")
        if not genesis_block:
            continue
        backends = coin.get("blockbook", []) + coin.get("bitcore", [])
        for backend in backends:
            print("checking", backend, "... ", end="", flush=True)
            try:
                j = requests.get(backend + "/api/block-index/0").json()
                if j["blockHash"] != genesis_block:
                    raise RuntimeError("genesis block mismatch")
            except Exception as e:
                print(e)
                check_passed = False
            else:
                print("OK")
    return check_passed


# ====== coindefs generators ======


def convert_icon(icon):
    """Convert PIL icon to TOIF format"""
    # TODO: move this to python-trezor at some point
    DIM = 32
    icon = icon.resize((DIM, DIM), Image.LANCZOS)
    # remove alpha channel, replace with black
    bg = Image.new("RGBA", icon.size, (0, 0, 0, 255))
    icon = Image.alpha_composite(bg, icon)
    # process pixels
    pix = icon.load()
    data = bytes()
    for y in range(DIM):
        for x in range(DIM):
            r, g, b, _ = pix[x, y]
            c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | ((b & 0xF8) >> 3)
            data += struct.pack(">H", c)
    z = zlib.compressobj(level=9, wbits=10)
    zdata = z.compress(data) + z.flush()
    zdata = zdata[2:-4]  # strip header and checksum
    return zdata


def coindef_from_dict(coin):
    proto = CoinDef()
    for fname, _, fflags in CoinDef.FIELDS.values():
        val = coin.get(fname)
        if val is None and fflags & protobuf.FLAG_REPEATED:
            val = []
        elif fname == "signed_message_header":
            val = val.encode("utf-8")
        elif fname == "hash_genesis_block":
            val = binascii.unhexlify(val)
        setattr(proto, fname, val)

    return proto


def serialize_coindef(proto, icon):
    proto.icon = icon
    buf = io.BytesIO()
    protobuf.dump_message(buf, proto)
    return buf.getvalue()


def sign(data):
    h = sha256(data).digest()
    sign_key = ed25519.SigningKey(b"A" * 32)
    return sign_key.sign(h)


# ====== click command handlers ======


@click.group()
def cli():
    pass


@cli.command()
@click.option(
    "--check-missing-support/--no-check-missing-support",
    "-s",
    help="Fail if support info for a coin is missing",
)
@click.option(
    "--backend-check/--no-backend-check",
    "-b",
    help="Also check blockbook/bitcore responses",
)
def check(check_missing_support, backend_check):
    """Validate coin definitions.

    Checks that every btc-like coin is properly filled out, reports address collisions
    and missing support information.
    """
    if backend_check and requests is None:
        raise click.ClickException("You must install requests for backend check")

    defs = coin_info.get_all()
    all_checks_passed = True

    print("Checking BTC-like coins...")
    if not check_btc(defs.coins):
        all_checks_passed = False

    print("Checking support data...")
    support_data = coin_info.get_support_data()
    if not check_support(defs, support_data, fail_missing=check_missing_support):
        all_checks_passed = False

    if backend_check:
        print("Checking backend responses...")
        if not check_backends(defs.coins):
            all_checks_passed = False

    if not all_checks_passed:
        print("Some checks failed.")
        sys.exit(1)
    else:
        print("Everything is OK.")


@cli.command()
@click.option("-o", "--outfile", type=click.File(mode="w"), default="./coins.json")
def coins_json(outfile):
    """Generate coins.json for consumption in python-trezor and Connect/Wallet"""
    coins = coin_info.get_all().coins
    support_info = coin_info.support_info(coins)
    by_name = {}
    for coin in coins:
        coin["support"] = support_info[coin["key"]]
        by_name[coin["name"]] = coin

    with outfile:
        json.dump(by_name, outfile, indent=4, sort_keys=True)
        outfile.write("\n")


@cli.command()
@click.option("-o", "--outfile", type=click.File(mode="w"), default="./coindefs.json")
def coindefs(outfile):
    """Generate signed coin definitions for python-trezor and others

    This is currently unused but should enable us to add new coins without having to
    update firmware.
    """
    coins = coin_info.get_all().coins
    coindefs = {}
    for coin in coins:
        key = coin["key"]
        icon = Image.open(coin["icon"])
        ser = serialize_coindef(coindef_from_dict(coin), convert_icon(icon))
        sig = sign(ser)
        definition = binascii.hexlify(sig + ser).decode("ascii")
        coindefs[key] = definition

    with outfile:
        json.dump(coindefs, outfile, indent=4, sort_keys=True)
        outfile.write("\n")


@cli.command()
@click.argument("paths", metavar="[path]...", nargs=-1)
def render(paths):
    """Generate source code from Jinja2 templates.

    For every "foo.bar.j2" filename passed, runs the template and
    saves the result as "foo.bar".

    For every directory name passed, processes all ".j2" files found
    in that directory.

    If no arguments are given, processes the current directory.
    """
    if not CAN_RENDER:
        raise click.ClickException("Please install 'mako' and 'munch'")

    if not paths:
        paths = ["."]

    files = []
    for path in paths:
        if not os.path.exists(path):
            click.echo("Path {} does not exist".format(path))
        elif os.path.isdir(path):
            files += glob.glob(os.path.join(path, "*.mako"))
        else:
            files.append(path)

    defs = coin_info.get_all()
    versions = coin_info.latest_releases()
    support_info = coin_info.support_info(defs, erc20_versions=versions)

    # munch dicts - make them attribute-accessable
    for key, value in defs.items():
        defs[key] = [Munch(coin) for coin in value]
    for key, value in support_info.items():
        support_info[key] = Munch(value)

    for file in files:
        if not file.endswith(".mako"):
            click.echo("File {} does not end with .mako".format(file))
        else:
            target = file[: -len(".mako")]
            click.echo("Rendering {} => {}".format(file, target))
            try:
                render_file(target, defs, support_info)
            except Exception as e:
                click.echo("Error occured: {}".format(e))
                raise


if __name__ == "__main__":
    cli()
