"""Only trusted server-side approval handlers may create TurnGrants."""
from dataclasses import dataclass

from .catalog import identity


@dataclass
class TurnGrant:
    job_id: str
    session_id: str
    revision: int
    context_identity: tuple
    filesystem_scope: object = None
    active: bool = True

    def matches(self, job, context):
        return (self.active and self.job_id == job.job_id and self.session_id == job.session_id
                and self.revision == job.revision and self.context_identity == identity(context))


def revoke_all(job):
    for grant in job.grants:
        grant.active = False
    job.grants.clear()
