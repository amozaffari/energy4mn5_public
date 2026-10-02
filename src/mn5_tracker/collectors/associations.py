"""Current Slurm associations (`sacctmgr show assoc`) and Unix group membership (`id -Gn`)."""

from dataclasses import dataclass

from mn5_tracker.collectors import ParseError


GROUPS_COMMAND = "id -Gn"
ASSOCIATION_FIELDS = ("Account", "Partition", "QOS", "User")


@dataclass(frozen=True)
class Association:
    account: str
    partitions: str
    qos: str


def build_associations_command(user: str) -> str:
    return f"sacctmgr -n -P show assoc user={user} format={','.join(ASSOCIATION_FIELDS)}"


def parse_associations(text: str) -> list[Association]:
    """One row per account; a user can hold several partition-specific rows per account."""
    by_account: dict[str, tuple[set[str], set[str]]] = {}

    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue

        fields = line.split("|")

        if len(fields) != len(ASSOCIATION_FIELDS):
            raise ParseError(f"sacctmgr line {line_number}: unexpected shape: {line!r}")

        account, partition, qos, _user = fields
        partitions, qos_names = by_account.setdefault(account, (set(), set()))

        if partition:
            partitions.add(partition)

        qos_names.update(name for name in qos.split(",") if name)

    return [
        Association(
            account=account,
            partitions=",".join(sorted(partitions)),
            qos=",".join(sorted(qos_names)),
        )
        for account, (partitions, qos_names) in sorted(by_account.items())
    ]


def parse_groups(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]

    if len(lines) != 1:
        raise ParseError(f"id -Gn: expected one line, got {len(lines)}")

    return lines[0].split()
