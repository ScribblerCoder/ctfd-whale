import fcntl
import warnings
from datetime import datetime, timedelta

import requests
from flask import Blueprint, render_template, session, current_app, request
from flask_apscheduler import APScheduler

from CTFd.api import CTFd_API_v1
from CTFd.plugins import (
    register_plugin_assets_directory,
    register_admin_plugin_menu_bar,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES
from CTFd.utils import get_config, set_config
from CTFd.utils.decorators import admins_only
from CTFd.models import db

from .api import user_namespace, admin_namespace, AdminContainers, AdminCheatingAttempts
from .challenge_type import DynamicValueDockerChallenge
from .utils.checks import WhaleChecks
from .utils.control import ControlUtil
from .utils.db import DBContainer
from .utils.docker import DockerUtils
from .utils.exceptions import WhaleWarning
from .utils.setup import setup_default_configs
from .utils.routers import Router
from .models import WhaleCheatingAttempt, WhaleSolvedFlag


def load(app):
    app.config['RESTX_ERROR_404_HELP'] = False
    # upgrade()
    plugin_name = __name__.split('.')[-1]
    set_config('whale:plugin_name', plugin_name)
    app.db.create_all()
    if not get_config("whale:setup"):
        setup_default_configs()

    register_plugin_assets_directory(
        app, base_path=f"/plugins/{plugin_name}/assets",
        endpoint='plugins.ctfd-whale.assets'
    )
    register_admin_plugin_menu_bar(
        title='Whale',
        route='/plugins/ctfd-whale/admin/settings'
    )

    DynamicValueDockerChallenge.templates = {
        "create": f"/plugins/{plugin_name}/assets/create.html",
        "update": f"/plugins/{plugin_name}/assets/update.html",
        "view": f"/plugins/{plugin_name}/assets/view.html",
    }
    DynamicValueDockerChallenge.scripts = {
        "create": "/plugins/ctfd-whale/assets/create.js",
        "update": "/plugins/ctfd-whale/assets/update.js",
        "view": "/plugins/ctfd-whale/assets/view.js",
    }
    CHALLENGE_CLASSES["dynamic_docker"] = DynamicValueDockerChallenge

    page_blueprint = Blueprint(
        "ctfd-whale",
        __name__,
        template_folder="templates",
        static_folder="assets",
        url_prefix="/plugins/ctfd-whale"
    )
    CTFd_API_v1.add_namespace(admin_namespace, path="/plugins/ctfd-whale/admin")
    CTFd_API_v1.add_namespace(user_namespace, path="/plugins/ctfd-whale")

    worker_config_commit = None

    @page_blueprint.route('/admin/settings')
    @admins_only
    def admin_list_configs():
        nonlocal worker_config_commit
        errors = WhaleChecks.perform()
        if not errors and get_config("whale:refresh") != worker_config_commit:
            worker_config_commit = get_config("whale:refresh")
            DockerUtils.init()
            Router.reset()
            set_config("whale:refresh", "false")
        return render_template('whale_config.html', errors=errors)

    @page_blueprint.route("/admin/containers")
    @admins_only
    def admin_list_containers():
        result = AdminContainers.get()
        view_mode = request.args.get('mode', session.get('view_mode', 'list'))
        session['view_mode'] = view_mode
        return render_template("whale_containers.html",
                               plugin_name=plugin_name,
                               containers=result['data']['containers'],
                               pages=result['data']['pages'],
                               curr_page=abs(request.args.get("page", 1, type=int)),
                               curr_page_start=result['data']['page_start'])

    @page_blueprint.route("/admin/images")
    @admins_only
    def admin_list_images():
        """Admin page for viewing Docker images - static HTML only"""
        prefix = get_config("whale:docker_image_prefix", "")
        error_message = None if prefix else "No image prefix configured."
        
        return render_template("whale_images.html",
                            plugin_name=plugin_name,
                            prefix=prefix,
                            error_message=error_message)

    @page_blueprint.route("/admin/cheating")
    @admins_only
    def admin_list_cheating():
        """Admin page for viewing cheating detection results"""
        page = abs(request.args.get("page", 1, type=int))
        results_per_page = abs(request.args.get("per_page", 20, type=int))
        page_start = results_per_page * (page - 1)
        page_end = results_per_page * (page - 1) + results_per_page

        # Get cheating attempts
        total_count = WhaleCheatingAttempt.query.count()
        cheating_attempts = WhaleCheatingAttempt.query.order_by(
            WhaleCheatingAttempt.attempt_time.desc()
        ).slice(page_start, page_end).all()

        # Calculate statistics
        total_attempts = total_count
        unique_cheaters = db.session.query(WhaleCheatingAttempt.cheater_user_id).distinct().count()
        unique_victims = db.session.query(WhaleCheatingAttempt.victim_user_id).distinct().count()
        affected_challenges = db.session.query(WhaleCheatingAttempt.challenge_id).distinct().count()

        return render_template("whale_cheating.html",
                               plugin_name=plugin_name,
                               cheating_attempts=cheating_attempts,
                               total_attempts=total_attempts,
                               unique_cheaters=unique_cheaters,
                               unique_victims=unique_victims,
                               affected_challenges=affected_challenges,
                               pages=int(total_count / results_per_page) + (total_count % results_per_page > 0),
                               curr_page=page,
                               curr_page_start=page_start)

    def auto_clean_container():
        """Enhanced cleanup that manages containers and old solved flags"""
        with app.app_context():
            # Clean expired containers (existing logic)
            results = DBContainer.get_all_expired_container()
            for r in results:
                ControlUtil.try_remove_container(r.user_id)
            
            # Clean old solved flags (extended cheating detection cleanup)
            cheating_detection_period = int(get_config("whale:cheating_detection_period", "86400"))  # 24 hours default
            
            if cheating_detection_period > 0:  # Only clean if period is set (0 = keep forever)
                cutoff_time = datetime.now() - timedelta(seconds=cheating_detection_period)
                
                old_solved_flags = WhaleSolvedFlag.query.filter(
                    WhaleSolvedFlag.solved_time < cutoff_time
                ).all()
                
                if old_solved_flags:
                    for solved_flag in old_solved_flags:
                        db.session.delete(solved_flag)
                    
                    db.session.commit()
                    print(f"[Whale] Cleaned {len(old_solved_flags)} old solved flags (older than {cheating_detection_period} seconds)")
            
            # Clean old cheating attempts (optional - keep for longer analysis)
            cheating_log_retention = int(get_config("whale:cheating_log_retention", "2592000"))  # 30 days default
            
            if cheating_log_retention > 0:
                log_cutoff_time = datetime.now() - timedelta(seconds=cheating_log_retention)
                
                old_cheating_attempts = WhaleCheatingAttempt.query.filter(
                    WhaleCheatingAttempt.attempt_time < log_cutoff_time
                ).all()
                
                if old_cheating_attempts:
                    for attempt in old_cheating_attempts:
                        db.session.delete(attempt)
                    
                    db.session.commit()
                    print(f"[Whale] Cleaned {len(old_cheating_attempts)} old cheating attempt logs (older than {cheating_log_retention} seconds)")

    app.register_blueprint(page_blueprint)

    try:
        Router.check_availability()
        DockerUtils.init()
    except Exception:
        warnings.warn("Initialization Failed. Please check your configs.", WhaleWarning)

    try:
        lock_file = open("/tmp/ctfd_whale.lock", "w")
        lock_fd = lock_file.fileno()
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        scheduler = APScheduler()
        scheduler.init_app(app)
        scheduler.start()
        scheduler.add_job(
            id='whale-auto-clean', func=auto_clean_container,
            trigger="interval", seconds=10
        )

        print("[CTFd Whale] Started successfully with extended cheating detection and image management enabled")
    except IOError:
        pass