class MigrationContext:
    pass

class Operations:
    def __init__(self, migration_context: MigrationContext) -> None: ...
    def get_context(self) -> MigrationContext: ...
