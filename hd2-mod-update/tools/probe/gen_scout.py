"""Generate the read-only owner-scout addon v0.2.0.

v0.1 searched only for the entity-manager global via component maps and came
back empty.  The logs then showed the *direct* components (owner = game.dll
base, located by a fixed RVA + guard bytes) failing too, so every locator RVA
moved.  v0.2 tests both families against every candidate pointer:

  * 13 direct components, fingerprinted by the neighbouring records the addons
    already verify (152..544 bytes of context);
  * 14 known owner-slot offsets, checked against the component-map fingerprints.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
DATA = json.loads((HERE / "scout_direct.json").read_text(encoding="utf-8"))
SIGS = json.loads((HERE / "scout_signatures.json").read_text(encoding="utf-8"))
NL = chr(10)

direct_lua = "local direct={" + NL
for label, d in sorted(DATA["direct"].items()):
    guards = ", ".join("{off=%d, bytes=unhex(%s)}" % (g["offset"], json.dumps(g["bytes"]))
                       for g in d["guards"])
    direct_lua += ('  {label=%s, size=%d, head=unhex(%s), original=unhex(%s), old_rva=0x%X, guards={%s}},'
                   % (json.dumps(label), d["record_size"], json.dumps(d["original"][:16]),
                      json.dumps(d["original"]), d["old_rva"], guards)) + NL
direct_lua += "}" + NL

sig_lua = "local signatures={" + NL
for name, v in SIGS.items():
    sig_lua += ('  {name=%s, offset=%d, bytes=unhex(%s)},'
                % (json.dumps(name), v["offset"], json.dumps(v["hex"]))) + NL
sig_lua += "}" + NL

slot_lua = "local owner_slots={" + ",".join("0x%X" % s for s in DATA["slots"]) + "}" + NL

template = pathlib.Path("scout_template_v4.lua").read_text(encoding="utf-8")
out = (template.replace("INSERT_DIRECT", direct_lua)
               .replace("INSERT_SIGNATURES", sig_lua)
               .replace("INSERT_SLOTS", slot_lua))
for token in ("INSERT_DIRECT", "INSERT_SIGNATURES", "INSERT_SLOTS"):
    assert token not in out, token
assert out.index("local function unhex") < out.index("local direct=")
(HERE / "owner_scout.lua").write_text(out, encoding="utf-8")
print("wrote owner_scout.lua", len(out), "chars; direct=%d maps=%d slots=%d"
      % (len(DATA["direct"]), len(SIGS), len(DATA["slots"])))
