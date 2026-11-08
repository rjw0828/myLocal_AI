"""Fixed, public HTTP-facing errors. No underlying SQL/process messages."""


class MCPServiceError(RuntimeError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status
