from ..validation import digest
from .design import InterimDesigns
from .study import InterimStudy
from .validate import InterimValidation


class InterimAnalysis:
    """Pre-registered group sequential interim analysis for batch-randomized studies."""

    def __init__(self, database, registry, ingestion):
        self.designs_store = InterimDesigns(database, registry)
        self.study = InterimStudy(database, registry, ingestion, self.designs_store)
        self.validation = InterimValidation(database, self.designs_store)

    def register_design(self, **fields):
        return self.designs_store.register(**fields)

    def design(self, design_id):
        payload = self.designs_store.get(design_id)
        return {
            "id": payload["id"],
            "digest": digest(payload),
            "design": payload,
            **self.study.state(payload["id"]),
        }

    def designs(self):
        return self.designs_store.list()

    def look(self, **kwargs):
        return self.study.look(**kwargs)

    def report(self, **kwargs):
        return self.study.report(**kwargs)

    def validate(self, **kwargs):
        return self.validation.validate(**kwargs)
