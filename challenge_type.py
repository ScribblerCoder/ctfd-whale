from flask import Blueprint, request

from CTFd.models import (
    db,
    Flags,
)
from CTFd.plugins.challenges import BaseChallenge
from CTFd.plugins.dynamic_challenges import DynamicValueChallenge
from CTFd.plugins.flags import get_flag_class
from CTFd.utils import user as current_user
from .models import WhaleContainer, DynamicDockerChallenge, WhaleCheatingAttempt, WhaleSolvedFlag
from .utils.control import ControlUtil


class DynamicValueDockerChallenge(BaseChallenge):
    id = "dynamic_docker"  # Unique identifier used to register challenges
    name = "dynamic_docker"  # Name of a challenge type
    # Blueprint used to access the static_folder directory.
    blueprint = Blueprint(
        "ctfd-whale-challenge",
        __name__,
        template_folder="templates",
        static_folder="assets",
    )
    challenge_model = DynamicDockerChallenge

    @classmethod
    def read(cls, challenge):
        challenge = DynamicDockerChallenge.query.filter_by(id=challenge.id).first()
        data = {
            "id": challenge.id,
            "name": challenge.name,
            "value": challenge.value,
            "initial": challenge.initial,
            "decay": challenge.decay,
            "minimum": challenge.minimum,
            "description": challenge.description,
            "category": challenge.category,
            "state": challenge.state,
            "max_attempts": challenge.max_attempts,
            "type": challenge.type,
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
        }
        return data

    @classmethod
    def update(cls, challenge, request):
        data = request.form or request.get_json()

        for attr, value in data.items():
            # We need to set these to floats so that the next operations don't operate on strings
            if attr in ("initial", "minimum", "decay"):
                value = float(value)
            if attr == 'dynamic_score':
                value = int(value)
            # Handle new flag mode fields
            if attr in ('flag_mode', 'flag_static_prefix'):
                # Ensure the fields exist on the challenge object
                if hasattr(challenge, attr):
                    setattr(challenge, attr, value)
                continue
            setattr(challenge, attr, value)

        if challenge.dynamic_score == 1:
            return DynamicValueChallenge.calculate_value(challenge)

        db.session.commit()
        return challenge

    @classmethod
    def create(cls, request):
        """
        This method is used to process the challenge creation request.
        """
        data = request.form or request.get_json()
        
        # Extract flag mode data
        flag_mode = data.get('flag_mode', 'dynamic')
        flag_static_prefix = data.get('flag_static_prefix', '')

        challenge = cls.challenge_model(**data)
        
        # Set the new flag mode fields
        challenge.flag_mode = flag_mode
        challenge.flag_static_prefix = flag_static_prefix

        db.session.add(challenge)
        db.session.commit()

        return challenge

    @classmethod
    def attempt(cls, challenge, request):
        data = request.form or request.get_json()
        submission = data["submission"].strip()
        current_user_obj = current_user.get_current_user()
        user_id = current_user_obj.id

        # Check for manual flags first (for static flag mode)
        flags = Flags.query.filter_by(challenge_id=challenge.id).all()
        if len(flags) > 0:
            for flag in flags:
                if get_flag_class(flag.type).compare(flag, submission):
                    return True, "Correct"
            return False, "Incorrect"
        
        # For dynamic and half-dynamic flags, check container flag
        q = db.session.query(WhaleContainer)
        q = q.filter(WhaleContainer.user_id == user_id)
        q = q.filter(WhaleContainer.challenge_id == challenge.id)
        user_container = q.first()
        
        if not user_container:
            return False, "Please solve it during the container is running"

        # Check if the submitted flag matches the user's own flag
        if user_container.flag == submission:
            return True, "Correct"
        
        # ENHANCED CHEATING DETECTION: Check both active containers and solved flags
        flag_owner_id = WhaleContainer.find_flag_owner(submission, challenge.id)
        
        if flag_owner_id and flag_owner_id != user_id:
            # This is a cheating attempt! Log it.
            cls._log_cheating_attempt(
                cheater_user_id=user_id,
                victim_user_id=flag_owner_id,
                challenge_id=challenge.id,
                submitted_flag=submission,
                request_obj=request
            )
            return False, "Incorrect"
        
        return False, "Incorrect"

    @classmethod
    def _log_cheating_attempt(cls, cheater_user_id, victim_user_id, challenge_id, submitted_flag, request_obj):
        """Log a cheating attempt to the database"""
        try:
            # Get IP address and user agent from request
            cheater_ip = request_obj.environ.get('HTTP_X_FORWARDED_FOR', 
                                               request_obj.environ.get('HTTP_X_REAL_IP', 
                                               request_obj.environ.get('REMOTE_ADDR')))
            user_agent = request_obj.environ.get('HTTP_USER_AGENT', '')
            
            # Create cheating attempt record
            cheating_attempt = WhaleCheatingAttempt(
                cheater_user_id=cheater_user_id,
                victim_user_id=victim_user_id,
                challenge_id=challenge_id,
                submitted_flag=submitted_flag,
                cheater_ip=cheater_ip,
                user_agent=user_agent
            )
            
            db.session.add(cheating_attempt)
            db.session.commit()
            
            # Optional: Log to CTFd's logging system
            from CTFd.utils import logging
            logging.log(
                'whale_cheating', 
                f'Cheating attempt detected: User {cheater_user_id} submitted User {victim_user_id}\'s flag for challenge {challenge_id}',
                cheater_id=cheater_user_id,
                victim_id=victim_user_id,
                challenge_id=challenge_id,
                flag=submitted_flag[:10] + "..." if len(submitted_flag) > 10 else submitted_flag
            )
            
        except Exception as e:
            # Don't let logging errors break the challenge attempt
            print(f"Error logging cheating attempt: {e}")

    @classmethod
    def solve(cls, user, team, challenge, request):
        """Enhanced solve method that preserves flags for extended cheating detection"""
        # Get the user's container and flag before calling parent solve
        user_id = user.id
        container = WhaleContainer.query.filter_by(
            user_id=user_id,
            challenge_id=challenge.id
        ).first()
        
        if container:
            # Check if this flag was already stored (avoid duplicates)
            existing_solved_flag = WhaleSolvedFlag.query.filter_by(
                user_id=user_id,
                challenge_id=challenge.id,
                flag=container.flag
            ).first()
            
            if not existing_solved_flag:
                # Store the solved flag for extended cheating detection
                solved_flag = WhaleSolvedFlag(
                    user_id=user_id,
                    challenge_id=challenge.id,
                    flag=container.flag,
                    container_uuid=container.uuid
                )
                db.session.add(solved_flag)
                db.session.commit()
                
                print(f"[Whale] Stored solved flag for user {user_id}, challenge {challenge.id} for extended cheating detection")
        
        # Call parent solve method
        super().solve(user, team, challenge, request)

        if challenge.dynamic_score == 1:
            DynamicValueChallenge.calculate_value(challenge)

    @classmethod
    def delete(cls, challenge):
        # Clean up all related data when challenge is deleted
        for container in WhaleContainer.query.filter_by(
            challenge_id=challenge.id
        ).all():
            ControlUtil.try_remove_container(container.user_id)
        
        # Clean up solved flags
        WhaleSolvedFlag.query.filter_by(challenge_id=challenge.id).delete()
        
        # Clean up cheating attempts
        WhaleCheatingAttempt.query.filter_by(challenge_id=challenge.id).delete()
        
        db.session.commit()
        super().delete(challenge)