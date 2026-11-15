from pathlib import Path
from flask import Flask, jsonify, render_template
from werkzeug.exceptions import HTTPException
from .store import Store
from .layers import Layers
from .timeline import Timeline
from .tween import Tween
from .palettes import Palettes
from .tile_palettes import TilePalettes
from .tilemaps import Tilemaps
from .jobs import Jobs
from .recipes import Recipes
from .api import create_api
from .validate import Conflict


def create_app(data_dir=None):
    root = Path(__file__).resolve().parents[1]
    app = Flask(__name__, template_folder=str(root / 'templates'), static_folder=str(root / 'static'))
    app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024
    app.json.ensure_ascii = False
    store = Store(data_dir or root / 'data')
    layers = Layers(store)
    timeline = Timeline(store)
    tween = Tween(store)
    palettes = Palettes(store)
    tile_palettes = TilePalettes(store)
    tilemaps = Tilemaps(store)
    jobs = Jobs(store)
    recipes = Recipes(store)
    recipes.ensure_defaults()
    app.extensions.update(store=store, layers=layers, timeline=timeline, tween=tween, palettes=palettes, tile_palettes=tile_palettes, tilemaps=tilemaps, jobs=jobs, recipes=recipes)
    app.register_blueprint(create_api(store, layers, timeline, tween, palettes, tile_palettes, tilemaps, jobs, recipes))

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.errorhandler(Conflict)
    def conflict(error):
        return jsonify({'error': str(error), 'type': 'conflict'}), 409

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify({'error': str(error), 'type': 'invalid'}), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        message = '单次上传不能超过64MB' if error.code == 413 else error.description
        return jsonify({'error': message}), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception('项目操作失败')
        return jsonify({'error': '处理失败，请检查服务日志'}), 500

    @app.after_request
    def response_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if response.mimetype == 'application/json':
            response.headers['Cache-Control'] = 'no-store'
        return response

    return app
