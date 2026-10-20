# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from flask import Blueprint

main_bp = Blueprint('main', __name__)

from . import dhcp4, dhcp6, system, options, ha, client_classes, instances

# Register the nested blueprints onto the main blueprint so they are automatically picked up by app factory
main_bp.register_blueprint(system.system_bp)
main_bp.register_blueprint(dhcp4.dhcp4_bp)
main_bp.register_blueprint(dhcp6.dhcp6_bp)
main_bp.register_blueprint(options.options_bp)
main_bp.register_blueprint(ha.ha_bp)
main_bp.register_blueprint(client_classes.client_classes_bp)
main_bp.register_blueprint(instances.instances_bp)
