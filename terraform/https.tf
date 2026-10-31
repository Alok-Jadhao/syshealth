# The dashboard at your own address over HTTPS, when domain_name is set: a
# network load balancer that holds the certificate, on the fixed address your
# DNS host's A record points at. The address and the certificate are made
# once, by domain/ (../aws.sh domain); this file only uses them, so they
# outlive every fleet.
#
# Open to anyone, unlike port 5000: an audience opens it on their own phones.
# What keeps it private is the server, which shows nothing without the token
# or the watch-only code. HTTPS only: there is nothing on port 80 to redirect
# from, so give people the https:// link.

locals {
  https = var.domain_name != null
}

# Fails the plan with "no matching EC2 EIP found" if domain/ has not been
# applied for this name, in this region.
data "aws_eip" "dashboard" {
  count = local.https ? 1 : 0

  filter {
    name   = "tag:SysHealthAddress"
    values = [var.domain_name]
  }
}

# Fails the plan with "no matching ACM certificate" if domain/ has not been
# applied, or its certificate is still waiting for the CNAME at your DNS host.
data "aws_acm_certificate" "dashboard" {
  count       = local.https ? 1 : 0
  domain      = var.domain_name
  statuses    = ["ISSUED"]
  most_recent = true
}

resource "aws_security_group" "lb" {
  count       = local.https ? 1 : 0
  name        = "${var.name_prefix}-lb"
  description = "SysHealth dashboard over HTTPS: anyone may connect, the page needs a code"
  vpc_id      = data.aws_vpc.default.id

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-lb" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "lb_https" {
  count             = local.https ? 1 : 0
  security_group_id = aws_security_group.lb[0].id
  description       = "The dashboard, for anyone with the address"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "lb_to_server" {
  count                        = local.https ? 1 : 0
  security_group_id            = aws_security_group.lb[0].id
  description                  = "On to the dashboard server, and nowhere else"
  referenced_security_group_id = aws_security_group.server.id
  from_port                    = 5000
  to_port                      = 5000
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "server_from_lb" {
  count                        = local.https ? 1 : 0
  security_group_id            = aws_security_group.server.id
  description                  = "Dashboard UI through the load balancer"
  referenced_security_group_id = aws_security_group.lb[0].id
  from_port                    = 5000
  to_port                      = 5000
  ip_protocol                  = "tcp"
}

# A network load balancer, as only that kind can take a fixed address. In the
# server's zone alone: it sends only to targets in its own zones, and the
# address can be in one place at a time.
resource "aws_lb" "dashboard" {
  count              = local.https ? 1 : 0
  name               = "${var.name_prefix}-dashboard"
  load_balancer_type = "network"
  security_groups    = [aws_security_group.lb[0].id]

  subnet_mapping {
    subnet_id     = aws_instance.server.subnet_id
    allocation_id = data.aws_eip.dashboard[0].id
  }

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-dashboard" })
}

resource "aws_lb_target_group" "dashboard" {
  count    = local.https ? 1 : 0
  name     = "${var.name_prefix}-dashboard"
  port     = 5000
  protocol = "TCP"
  vpc_id   = data.aws_vpc.default.id

  # Connections reach the server from the load balancer's own address, not
  # the viewer's, so the server's firewall names the load balancer and
  # nothing wider.
  preserve_client_ip = false

  # The page itself answers without a token, so it is what is checked. Two
  # good answers ten seconds apart: the server is ready soon after it boots.
  health_check {
    protocol            = "HTTP"
    path                = "/"
    matcher             = "200"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  # Nothing is long-lived here: no reason to make destroy wait five minutes.
  deregistration_delay = 5

  tags = local.common_tags
}

resource "aws_lb_target_group_attachment" "dashboard" {
  count            = local.https ? 1 : 0
  target_group_arn = aws_lb_target_group.dashboard[0].arn
  target_id        = aws_instance.server.id
  port             = 5000
}

resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.dashboard[0].arn
  port              = 443
  protocol          = "TLS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = data.aws_acm_certificate.dashboard[0].arn

  # The bytes go on to the server as they are, and it speaks HTTP/1.1 only:
  # offer browsers nothing newer.
  alpn_policy = "HTTP1Only"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.dashboard[0].arn
  }
}
