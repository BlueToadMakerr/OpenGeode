import os
import sys
import importlib.util

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTERS_DIR = os.path.join(BASE_DIR, "routers")
OPENGEODE_DIR = os.path.join(BASE_DIR, "routers/OpenGeode")

all_routers = []

def load_directory_modules(directory_path, namespace_prefix="routers"):
    if not os.path.exists(directory_path):
        return

    for filename in os.listdir(directory_path):
        if filename.endswith(".py") and filename != "__init__.py":
            module_name = filename[:-3]
            full_sys_path = f"{namespace_prefix}.{module_name}"
            
            if full_sys_path in sys.modules:
                module = sys.modules[full_sys_path]
                if hasattr(module, "router") and module.router not in all_routers:
                    all_routers.append(module.router)
                continue
                
            file_path = os.path.join(directory_path, filename)
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                
                sys.modules[full_sys_path] = module
                spec.loader.exec_module(module)
                
                if hasattr(module, "router"):
                    all_routers.append(getattr(module, "router"))

load_directory_modules(ROUTERS_DIR)

load_directory_modules(OPENGEODE_DIR)
