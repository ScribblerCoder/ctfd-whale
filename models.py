import random
import re
import uuid
from datetime import datetime

from jinja2 import Template

from CTFd.utils import get_config
from CTFd.models import db
from CTFd.plugins.dynamic_challenges import DynamicChallenge


class WhaleConfig(db.Model):
    key = db.Column(db.String(length=128), primary_key=True)
    value = db.Column(db.Text)

    def __init__(self, key, value):
        self.key = key
        self.value = value

    def __repr__(self):
        return "<WhaleConfig {0} {1}>".format(self.key, self.value)


class WhaleRedirectTemplate(db.Model):
    key = db.Column(db.String(20), primary_key=True)
    frp_template = db.Column(db.Text)
    access_template = db.Column(db.Text)

    def __init__(self, key, access_template, frp_template):
        self.key = key
        self.access_template = access_template
        self.frp_template = frp_template

    def __repr__(self):
        return "<WhaleRedirectTemplate {0}>".format(self.key)


class DynamicDockerChallenge(DynamicChallenge):
    __mapper_args__ = {"polymorphic_identity": "dynamic_docker"}
    id = db.Column(
        db.Integer, db.ForeignKey("dynamic_challenge.id", ondelete="CASCADE"), primary_key=True
    )

    memory_limit = db.Column(db.Text, default="128m")
    cpu_limit = db.Column(db.Float, default=0.5)
    dynamic_score = db.Column(db.Integer, default=0)

    docker_image = db.Column(db.Text, default=0)
    redirect_type = db.Column(db.Text, default=0)
    redirect_port = db.Column(db.Integer, default=0)
    
    # New fields for half-dynamic flags
    flag_mode = db.Column(db.String(20), default="dynamic")  # "static", "dynamic", "half_dynamic"
    flag_static_prefix = db.Column(db.String(100), default="")

    def __init__(self, *args, **kwargs):
        kwargs["initial"] = kwargs["value"]
        super(DynamicDockerChallenge, self).__init__(**kwargs)


class WhaleContainer(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(None, db.ForeignKey("users.id"))
    challenge_id = db.Column(None, db.ForeignKey("challenges.id"))
    start_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    renew_count = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.Integer, default=1)
    uuid = db.Column(db.String(256))
    port = db.Column(db.Integer, nullable=True, default=0)
    flag = db.Column(db.String(128), nullable=False)

    # Relationships
    user = db.relationship(
        "Users", foreign_keys="WhaleContainer.user_id", lazy="select")
    challenge = db.relationship(
        "DynamicDockerChallenge", foreign_keys="WhaleContainer.challenge_id", lazy="select"
    )

    @property
    def http_subdomain(self):
        return Template(get_config(
            'whale:template_http_subdomain', '{{ container.uuid }}'
        )).render(container=self)

    def __init__(self, user_id, challenge_id):
        self.user_id = user_id
        self.challenge_id = challenge_id
        self.start_time = datetime.now()
        self.renew_count = 0
        self.uuid = str(uuid.uuid4())
        
        # Generate flag based on challenge flag mode
        self.flag = self._generate_flag()

    def _generate_flag(self):
        """Generate flag based on the challenge's flag mode"""
        from .models import DynamicDockerChallenge
        
        challenge = DynamicDockerChallenge.query.filter_by(id=self.challenge_id).first()
        
        if not challenge:
            # Fallback to default dynamic flag generation
            return Template(get_config(
                'whale:template_chall_flag', '{{ "flag{"+uuid.uuid4()|string+"}" }}'
            )).render(container=self, uuid=uuid, random=random, get_config=get_config)
        
        flag_mode = getattr(challenge, 'flag_mode', 'dynamic')
        
        if flag_mode == "static":
            # For static flags, we still need to return something
            # This should be handled by the manual flag system in CTFd
            return Template(get_config(
                'whale:template_chall_flag', '{{ "flag{"+uuid.uuid4()|string+"}" }}'
            )).render(container=self, uuid=uuid, random=random, get_config=get_config)
            
        elif flag_mode == "half_dynamic":
            # Generate half-dynamic flag using the global flag template
            prefix = getattr(challenge, 'flag_static_prefix', '')
            if prefix and not prefix.endswith("_"):
                prefix = prefix + "_"
            
            # Generate dynamic part (8 character random string)
            dynamic_part = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))
            
            # Construct the half-dynamic content: prefix + dynamic_part
            half_dynamic_content = prefix + dynamic_part
            
            # Get the flag template from config
            flag_template = get_config('whale:template_chall_flag', '{{ "flag{"+uuid.uuid4()|string+"}" }}')
            
            # Parse the template to extract the format and replace content between {}
            import re
            
            # First render the template to get the actual flag format
            temp_context = {
                'container': self,
                'uuid': uuid,
                'random': random,
                'get_config': get_config,
            }
            rendered_template = Template(flag_template).render(**temp_context)
            
            # Now replace whatever is between {} with our half-dynamic content
            # This handles any format like flag{...}, CTF{...}, EVENTNAME{...}, etc.
            pattern = r'^([^{]*\{)([^}]*)(\}.*)'
            match = re.match(pattern, rendered_template)
            
            if match:
                    # Extract prefix (before {}), content (between {}), and suffix (after {})
                    template_prefix = match.group(1)  # e.g., "flag{" or "CTFNAME{"
                    template_suffix = match.group(3)   # e.g., "}" or "}_END"
                    
                    # Construct final flag with our half-dynamic content
                    final_flag = template_prefix + half_dynamic_content + template_suffix
                    return final_flag
            else:
                # Fallback: if no {} found, just append our content
                return rendered_template + half_dynamic_content
        else:  # dynamic mode (default)
            return Template(get_config(
                'whale:template_chall_flag', '{{ "flag{"+uuid.uuid4()|string+"}" }}'
            )).render(container=self, uuid=uuid, random=random, get_config=get_config)

    @property
    def user_access(self):
        return Template(WhaleRedirectTemplate.query.filter_by(
            key=self.challenge.redirect_type
        ).first().access_template).render(container=self, get_config=get_config)

    @property
    def frp_config(self):
        return Template(WhaleRedirectTemplate.query.filter_by(
            key=self.challenge.redirect_type
        ).first().frp_template).render(container=self, get_config=get_config)

    def __repr__(self):
        return "<WhaleContainer ID:{0} {1} {2} {3} {4}>".format(self.id, self.user_id, self.challenge_id,
                                                                self.start_time, self.renew_count)
            

    @property
    def user_access(self):
        return Template(WhaleRedirectTemplate.query.filter_by(
            key=self.challenge.redirect_type
        ).first().access_template).render(container=self, get_config=get_config)

    @property
    def frp_config(self):
        return Template(WhaleRedirectTemplate.query.filter_by(
            key=self.challenge.redirect_type
        ).first().frp_template).render(container=self, get_config=get_config)

    def __repr__(self):
        return "<WhaleContainer ID:{0} {1} {2} {3} {4}>".format(self.id, self.user_id, self.challenge_id,
                                                                self.start_time, self.renew_count)