"""Command-line entry point."""
import argparse
import json
import sys

from .agent import run_agent_swarm
from .blackboard import Blackboard
from .config import load_config
from .fleet import HERE, Fleet, RUNTIME
from .horde import run_horde
from .serve import run_serve
from .swarm import run_ensemble, run_solo, run_swarm


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["up", "down", "status", "ask", "board",
                                        "serve", "horde"])
    ap.add_argument("--config", default=str(HERE / "swarm.toml"))
    ap.add_argument("--mode", default="swarm", choices=["swarm", "ensemble", "solo", "agent"])
    ap.add_argument("--member", default=None, help="for solo mode")
    ap.add_argument("--problem", default=None)
    ap.add_argument("--judge", default=None, help="ensemble mode judge member")
    ap.add_argument("--roles", default="{}", help='JSON, e.g. {"planner":"atlas","critic":"atlas"}')
    ap.add_argument("--bb", default=str(RUNTIME / "blackboard.sqlite"))
    args = ap.parse_args()

    cfg, members, order = load_config(args.config)
    fleet = Fleet(cfg, members, order)

    if args.command == "up":
        fleet.up()
        return
    if args.command == "down":
        fleet.down()
        return
    if args.command == "status":
        fleet.status()
        return
    if args.command in ("serve", "horde"):
        bb = Blackboard(str(RUNTIME / "blackboard.sqlite"), cfg.get("blackboard", {}))
        dropped = bb.prune()
        if dropped:
            print(f"[blackboard] pruned {dropped} entries "
                  f"(retention {bb.retention_days:g} days)")
        # Separate horde-only board: horde jobs write here and flush per job,
        # so external prompts never mix with the normal (serve) blackboard.
        bb_horde = Blackboard(str(RUNTIME / "blackboard_horde.sqlite"),
                              cfg.get("blackboard", {}))
        dropped = bb_horde.prune()
        if dropped:
            print(f"[blackboard] horde board pruned {dropped} entries "
                  f"(retention {bb_horde.retention_days:g} days)")
        state = {"cfg": cfg, "fleet": fleet, "bb": bb, "bb_horde": bb_horde,
                 "toml_path": args.config}
        if args.command == "serve":
            run_serve(state)
        else:
            run_horde(state)
        return
    if args.command == "board":
        bb = Blackboard(args.bb)
        for kind, member, snippet, ts in bb.tail():
            print(f"{ts} [{kind}/{member}] {snippet}")
        return

    # ask
    problem = args.problem or " ".join(sys.argv[3:])
    if not problem:
        ap.error("ask needs --problem or a positional question")
    bb = Blackboard(args.bb)
    roles = json.loads(args.roles)
    active = [n for n in order if members[n].enabled]
    if not active:
        print("no enabled members (check enabled=true in swarm.toml)", file=sys.stderr)
        sys.exit(1)
    if args.mode == "solo":
        name = args.member or active[0]
        final, status, details = run_solo(fleet, bb, problem, name)
        print(final)
    elif args.mode == "ensemble":
        role_j = [n for n in active if getattr(members[n], "role", "worker") == "judge"]
        judge = args.judge or (role_j[0] if role_j else active[-1])
        final, status, details = run_ensemble(fleet, bb, problem, active, judge)
        print(final)
    elif args.mode == "agent":
        tools = None  # CLI mode doesn't have tools yet
        final, status, details = run_agent_swarm(fleet, bb, problem, active, judge,
                                                 tools=tools)
        print(final)
    else:
        final, status, details = run_swarm(fleet, bb, problem, active, roles)
        print(final)


if __name__ == "__main__":
    main()
