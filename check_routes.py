from app.main import app

print(f"Total routes: {len(app.routes)}")
for route in app.routes:
    if hasattr(route, 'path'):
        methods = ", ".join(route.methods) if hasattr(route, 'methods') else "GET"
        print(f"{route.path} [{methods}]")
