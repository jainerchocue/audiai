import os

# Las pruebas nunca deben usar un proveedor remoto ni generar cargos.
os.environ.setdefault("AUDIAI_ROLE_PROVIDER", "mock")
