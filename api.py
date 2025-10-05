from datetime import datetime, timedelta
import csv
import io

from flask import request, make_response
from flask_restx import Namespace, Resource, abort

from CTFd.utils import get_config
from CTFd.utils import user as current_user
from CTFd.utils.decorators import admins_only, authed_only
from CTFd.models import db

from .decorators import challenge_visible, frequency_limited
from .utils.control import ControlUtil
from .utils.db import DBContainer
from .utils.routers import Router
from .utils.docker import DockerUtils
from .models import WhaleCheatingAttempt

admin_namespace = Namespace("ctfd-whale-admin")
user_namespace = Namespace("ctfd-whale-user")


@admin_namespace.errorhandler
@user_namespace.errorhandler
def handle_default(err):
    return {
        'success': False,
        'message': 'Unexpected things happened'
    }, 500


@admin_namespace.route('/container')
class AdminContainers(Resource):
    @staticmethod
    @admins_only
    def get():
        page = abs(request.args.get("page", 1, type=int))
        results_per_page = abs(request.args.get("per_page", 20, type=int))
        page_start = results_per_page * (page - 1)
        page_end = results_per_page * (page - 1) + results_per_page

        count = DBContainer.get_all_alive_container_count()
        containers = DBContainer.get_all_alive_container_page(
            page_start, page_end)

        return {'success': True, 'data': {
            'containers': containers,
            'total': count,
            'pages': int(count / results_per_page) + (count % results_per_page > 0),
            'page_start': page_start,
        }}

    @staticmethod
    @admins_only
    def patch():
        user_id = request.args.get('user_id', -1)
        challenge_id = request.args.get('challenge_id', -1)
        result, message = ControlUtil.try_renew_container(user_id=int(user_id), challenge_id=int(challenge_id))
        if not result:
            abort(403, message, success=False)
        return {'success': True, 'message': message}

    @staticmethod
    @admins_only
    def delete():
        user_id = request.args.get('user_id')
        challenge_id = request.args.get('challenge_id')
        result, message = ControlUtil.try_remove_container(user_id, challenge_id)
        return {'success': result, 'message': message}


@admin_namespace.route('/images')
class AdminImages(Resource):
    @staticmethod
    @admins_only
    def get():
        """Get all Docker images that match the configured prefix"""
        try:
            prefix = get_config("whale:docker_image_prefix", "")
            if not prefix:
                return {
                    'success': False,
                    'message': 'No image prefix configured. Please set whale:docker_image_prefix in settings.'
                }

            images = DockerUtils.get_images_by_prefix(prefix)
            return {
                'success': True,
                'data': {
                    'images': images,
                    'prefix': prefix,
                    'total': len(images)
                }
            }
        except Exception as e:
            return {
                'success': False,
                'message': f'Error fetching images: {str(e)}'
            }, 500


@admin_namespace.route('/images/list')
class AdminImagesList(Resource):
    @staticmethod
    @admins_only
    def get():
        """Get simple list of image names for dropdown"""
        try:
            prefix = get_config("whale:docker_image_prefix", "")
            if not prefix:
                return {
                    'success': False,
                    'message': 'No image prefix configured'
                }

            images = DockerUtils.get_images_by_prefix(prefix)
            image_names = [img['name'] for img in images]
            
            return {
                'success': True,
                'data': {
                    'images': image_names,
                    'prefix': prefix
                }
            }
        except Exception as e:
            return {
                'success': False,
                'message': f'Error fetching image list: {str(e)}'
            }, 500


@admin_namespace.route('/images/refresh')
class AdminImagesRefresh(Resource):
    @staticmethod
    @admins_only
    def post():
        """Refresh Docker images cache"""
        try:
            # Force refresh by pulling latest image information
            prefix = get_config("whale:docker_image_prefix", "")
            if not prefix:
                return {
                    'success': False,
                    'message': 'No image prefix configured'
                }

            # This will fetch fresh data from Docker daemon
            images = DockerUtils.get_images_by_prefix(prefix, force_refresh=True)
            
            return {
                'success': True,
                'message': f'Refreshed {len(images)} images with prefix "{prefix}"',
                'data': {
                    'count': len(images),
                    'prefix': prefix
                }
            }
        except Exception as e:
            return {
                'success': False,
                'message': f'Error refreshing images: {str(e)}'
            }, 500


@admin_namespace.route('/cheating')
class AdminCheatingAttempts(Resource):
    @staticmethod
    @admins_only
    def get():
        page = abs(request.args.get("page", 1, type=int))
        results_per_page = abs(request.args.get("per_page", 20, type=int))
        page_start = results_per_page * (page - 1)
        page_end = results_per_page * (page - 1) + results_per_page

        # Get all cheating attempts ordered by most recent
        total_count = WhaleCheatingAttempt.query.count()
        attempts = WhaleCheatingAttempt.query.order_by(
            WhaleCheatingAttempt.attempt_time.desc()
        ).slice(page_start, page_end).all()

        # Get statistics
        stats = {
            'total_attempts': total_count,
            'unique_cheaters': db.session.query(WhaleCheatingAttempt.cheater_user_id).distinct().count(),
            'unique_victims': db.session.query(WhaleCheatingAttempt.victim_user_id).distinct().count(),
            'affected_challenges': db.session.query(WhaleCheatingAttempt.challenge_id).distinct().count(),
        }

        return {'success': True, 'data': {
            'attempts': attempts,
            'stats': stats,
            'total': total_count,
            'pages': int(total_count / results_per_page) + (total_count % results_per_page > 0),
            'page_start': page_start,
        }}


@admin_namespace.route('/cheating/export')
class AdminCheatingExport(Resource):
    @staticmethod
    @admins_only
    def get():
        """Export cheating attempts as CSV"""
        # Get all cheating attempts
        attempts = WhaleCheatingAttempt.query.order_by(
            WhaleCheatingAttempt.attempt_time.desc()
        ).all()

        # Create CSV content
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow([
            'Timestamp', 'Cheater_ID', 'Cheater_Name', 'Victim_ID', 'Victim_Name',
            'Challenge_ID', 'Challenge_Name', 'Challenge_Category', 
            'Submitted_Flag', 'Cheater_IP', 'User_Agent'
        ])
        
        # Write data
        for attempt in attempts:
            writer.writerow([
                attempt.attempt_time.strftime('%Y-%m-%d %H:%M:%S'),
                attempt.cheater_user_id,
                attempt.cheater.name,
                attempt.victim_user_id,
                attempt.victim.name,
                attempt.challenge_id,
                attempt.challenge.name,
                attempt.challenge.category,
                attempt.submitted_flag,
                attempt.cheater_ip or '',
                attempt.user_agent or ''
            ])

        # Create response
        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = f'attachment; filename=cheating_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
        
        return response


@admin_namespace.route('/cheating/clear')
class AdminCheatingClear(Resource):
    @staticmethod
    @admins_only
    def post():
        """Clear old cheating records (older than 30 days)"""
        try:
            cutoff_date = datetime.now() - timedelta(days=30)
            
            # Count records to be deleted
            old_records = WhaleCheatingAttempt.query.filter(
                WhaleCheatingAttempt.attempt_time < cutoff_date
            ).count()
            
            # Delete old records
            WhaleCheatingAttempt.query.filter(
                WhaleCheatingAttempt.attempt_time < cutoff_date
            ).delete()
            
            db.session.commit()
            
            return {
                'success': True, 
                'message': f'Successfully cleared {old_records} old cheating records (older than 30 days)'
            }
            
        except Exception as e:
            db.session.rollback()
            return {
                'success': False,
                'message': f'Error clearing records: {str(e)}'
            }, 500


@admin_namespace.route('/cheating/stats')
class AdminCheatingStats(Resource):
    @staticmethod
    @admins_only
    def get():
        """Get detailed cheating statistics"""
        try:
            # Overall stats
            total_attempts = WhaleCheatingAttempt.query.count()
            unique_cheaters = db.session.query(WhaleCheatingAttempt.cheater_user_id).distinct().count()
            unique_victims = db.session.query(WhaleCheatingAttempt.victim_user_id).distinct().count()
            
            # Top cheaters
            top_cheaters = db.session.query(
                WhaleCheatingAttempt.cheater_user_id,
                db.func.count(WhaleCheatingAttempt.id).label('attempt_count')
            ).group_by(WhaleCheatingAttempt.cheater_user_id).order_by(
                db.func.count(WhaleCheatingAttempt.id).desc()
            ).limit(10).all()
            
            # Most targeted victims
            top_victims = db.session.query(
                WhaleCheatingAttempt.victim_user_id,
                db.func.count(WhaleCheatingAttempt.id).label('target_count')
            ).group_by(WhaleCheatingAttempt.victim_user_id).order_by(
                db.func.count(WhaleCheatingAttempt.id).desc()
            ).limit(10).all()
            
            # Most affected challenges
            affected_challenges = db.session.query(
                WhaleCheatingAttempt.challenge_id,
                db.func.count(WhaleCheatingAttempt.id).label('cheat_count')
            ).group_by(WhaleCheatingAttempt.challenge_id).order_by(
                db.func.count(WhaleCheatingAttempt.id).desc()
            ).limit(10).all()
            
            # Recent activity (last 7 days)
            week_ago = datetime.now() - timedelta(days=7)
            recent_attempts = WhaleCheatingAttempt.query.filter(
                WhaleCheatingAttempt.attempt_time >= week_ago
            ).count()
            
            return {
                'success': True,
                'data': {
                    'overall': {
                        'total_attempts': total_attempts,
                        'unique_cheaters': unique_cheaters,
                        'unique_victims': unique_victims,
                        'recent_attempts': recent_attempts
                    },
                    'top_cheaters': top_cheaters,
                    'top_victims': top_victims,
                    'affected_challenges': affected_challenges
                }
            }
            
        except Exception as e:
            return {
                'success': False,
                'message': f'Error getting stats: {str(e)}'
            }, 500


@user_namespace.route("/container")
class UserContainers(Resource):
    @staticmethod
    @authed_only
    @challenge_visible
    def get():
        user_id = current_user.get_current_user().id
        challenge_id = request.args.get('challenge_id')
        container = DBContainer.get_current_containers(user_id=user_id, challenge_id=challenge_id)
        if not container:
            return {'success': True, 'data': {}}
        timeout = int(get_config("whale:docker_timeout", "3600"))
        c = container.challenge # build a url for quick jump. todo: escape dash in categories and names.
        link = f'<a target="_blank" href="/challenges#{c.category}-{c.name}-{c.id}">{c.name}</a>'
        if int(container.challenge_id) != int(challenge_id):
            return abort(403, f'Container already started but not from this challenge ({link})', success=False)
        return {
            'success': True,
            'data': {
                'lan_domain': str(user_id) + "-" + container.uuid,
                'user_access': Router.access(container),
                'remaining_time': timeout - (datetime.now() - container.start_time).seconds,
            }
        }

    @staticmethod
    @authed_only
    @challenge_visible
    @frequency_limited
    def post():
        user_id = current_user.get_current_user().id
        challenge_id = request.args.get('challenge_id')
        ControlUtil.try_remove_container(user_id, challenge_id)

        current_count = DBContainer.get_all_alive_container_count()
        if int(get_config("whale:docker_max_container_count")) <= int(current_count):
            abort(403, 'Max container count exceed.', success=False)

        result, message = ControlUtil.try_add_container(
            user_id=user_id,
            challenge_id=challenge_id
        )
        if not result:
            abort(403, message, success=False)
        return {'success': True, 'message': message}

    @staticmethod
    @authed_only
    @challenge_visible
    @frequency_limited
    def patch():
        user_id = current_user.get_current_user().id
        challenge_id = request.args.get('challenge_id')
        docker_max_renew_count = int(get_config("whale:docker_max_renew_count", 5))
        container = DBContainer.get_current_containers(user_id, challenge_id)
        if container is None:
            abort(403, 'Instance not found.', success=False)
        if int(container.challenge_id) != int(challenge_id):
            abort(403, f'Container started but not from this challenge（{container.challenge.name}）', success=False)
        if container.renew_count >= docker_max_renew_count:
            abort(403, 'Max renewal count exceed.', success=False)
        result, message = ControlUtil.try_renew_container(user_id=user_id, challenge_id=challenge_id)
        return {'success': result, 'message': message}

    @staticmethod
    @authed_only
    @frequency_limited
    def delete():
        user_id = current_user.get_current_user().id
        challenge_id = request.args.get('challenge_id')
        result, message = ControlUtil.try_remove_container(user_id, challenge_id)
        if not result:
            abort(403, message, success=False)
        return {'success': True, 'message': message}