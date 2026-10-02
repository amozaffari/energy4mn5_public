"""Unit conversions between sacct billing, node-hours, GPU-hours and bsc_acct khours.

Canonical unit: node-hours per partition class. Two flavours are kept per job:

- `node_hours`: ElapsedRaw × NNodes, i.e. nodes occupied. Matches hand counts of "node-h used".
- `billed_node_hours`: billing TRES × ElapsedRaw / hw_threads_per_node, i.e. what the budget is
  charged. On ACC a 1-GPU job is billed a quarter node (billing=40), not a full node, so this can
  be lower than `node_hours`. It reconciles with bsc_acct khours to within ~0.1 %.
"""

from dataclasses import dataclass

from mn5_tracker.collectors.sacct import SacctJob
from mn5_tracker.config import NodeType


SECONDS_PER_HOUR = 3600
CORE_HOURS_PER_KHOUR = 1000


@dataclass(frozen=True)
class JobUsage:
    partition_class: str | None
    node_hours: float
    billed_node_hours: float
    gpu_hours_node: float
    gpu_hours_billed: float
    gpu_hours_requested: float
    core_hours_billed: float
    billing_units_per_node: float | None

    @property
    def khours_billed(self) -> float:
        return self.core_hours_billed / CORE_HOURS_PER_KHOUR


def partition_class(partition: str, node_types: dict[str, NodeType]) -> str | None:
    """Classify "acc", "gpp" or multi-partition strings such as "acc,gpp" (first match wins)."""
    for candidate in partition.split(","):
        for node_type in node_types.values():
            if candidate.startswith(node_type.partition_prefixes):
                return node_type.name

    return None


def billing_units(job: SacctJob) -> int:
    """`billing=` from AllocTRES; falls back to AllocCPUS when the TRES string is empty."""
    billing = job.tres.get("billing", "")

    return int(billing) if billing.isdigit() else job.alloc_cpus


def requested_gpus(job: SacctJob) -> int:
    gpus = job.tres.get("gres/gpu", "")

    return int(gpus) if gpus.isdigit() else 0


def compute_usage(job: SacctJob, node_types: dict[str, NodeType]) -> JobUsage:
    class_name = partition_class(job.partition, node_types)
    node_type = node_types.get(class_name) if class_name else None
    elapsed_hours = job.elapsed_s / SECONDS_PER_HOUR
    node_hours = elapsed_hours * job.n_nodes
    billing = billing_units(job)

    if node_type is None:
        return JobUsage(
            partition_class=class_name,
            node_hours=node_hours,
            billed_node_hours=0.0,
            gpu_hours_node=0.0,
            gpu_hours_billed=0.0,
            gpu_hours_requested=requested_gpus(job) * elapsed_hours,
            core_hours_billed=0.0,
            billing_units_per_node=None,
        )

    billed_node_hours = billing * elapsed_hours / node_type.hw_threads

    return JobUsage(
        partition_class=class_name,
        node_hours=node_hours,
        billed_node_hours=billed_node_hours,
        gpu_hours_node=node_hours * node_type.gpus,
        gpu_hours_billed=billed_node_hours * node_type.gpus,
        gpu_hours_requested=requested_gpus(job) * elapsed_hours,
        core_hours_billed=billed_node_hours * node_type.physical_cores,
        billing_units_per_node=billing / job.n_nodes if job.n_nodes else None,
    )


def khours_to_node_hours(khours: float, node_type: NodeType) -> float:
    """bsc_acct counts physical cores: ACC node-h = khours × 1000 / 80."""
    return khours * CORE_HOURS_PER_KHOUR / node_type.physical_cores


def node_hours_to_khours(node_hours: float, node_type: NodeType) -> float:
    return node_hours * node_type.physical_cores / CORE_HOURS_PER_KHOUR


def check_node_spec(job: SacctJob, usage: JobUsage, node_types: dict[str, NodeType]) -> str | None:
    """Return a warning when sacct bills more per node than the configured hardware threads."""
    node_type = node_types.get(usage.partition_class or "")

    if node_type is None or usage.billing_units_per_node is None:
        return None

    if usage.billing_units_per_node <= node_type.hw_threads:
        return None

    return (
        f"job {job.job_id}: billing {usage.billing_units_per_node:.0f}/node exceeds configured "
        f"hw_threads={node_type.hw_threads} for {node_type.name}; update node_types in tracker.yaml"
    )
