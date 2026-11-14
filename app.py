import os
from flipbook import create_app

app = create_app(os.environ.get('FLIPBOOK_DATA_DIR'))

if __name__ == '__main__':
    from waitress import serve
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', 8895))
    print(f'Flipbook Studio: http://{host}:{port}', flush=True)
    serve(app, host=host, port=port, threads=6)
