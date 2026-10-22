terraform {
  required_version = ">= 1.3"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  # Keyed by type so `terraform plan` reads as a list of sizes rather than
  # index numbers, and adding a size never renumbers the others. The index comes
  # along so each instance can be given a different subnet.
  instances = {
    for idx, t in var.instance_types : t => {
      type  = t
      index = idx
    }
  }

  # Capacity is per availability zone per instance type, so pinning everything to
  # one subnet means a single zone running short of t3 blocks the whole fleet with
  # InsufficientInstanceCapacity — and that error is retryable, so it presents as
  # an apply that hangs rather than one that fails. Spreading across the default
  # subnets asks a different zone each time.
  subnet_ids = (
    var.subnet_id != null ? [var.subnet_id] : data.aws_subnets.default.ids
  )

  # Pinned if asked for, otherwise Canonical's latest. See variables.tf: this
  # matters when a fleet is torn down and rebuilt expecting the same behaviour.
  ami_id = var.ami_id != null ? var.ami_id : data.aws_ami.ubuntu.id

  common_tags = merge(var.tags, {
    Project   = "syshealth"
    ManagedBy = "terraform"
  })
}

# The secret the server and agents share. Agents send it with every push, and
# the dashboard, cli.py and the server need it to start or stop a load test, so
# reaching port 5000 or 5001 is no longer enough on its own. It lives in the
# state (gitignored); read it with `terraform output -raw token`.
resource "random_password" "token" {
  length  = 32
  special = false
}

# The default VPC keeps this to one file with no NAT gateway to pay for. Server
# and agents share it, so they reach each other over private addresses.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Key pairs are per region, so changing region without creating one there is an
# easy mistake. Looking it up fails at plan time with "no matching EC2 Key Pair
# found" instead of part way through apply, once instances already exist.
data "aws_key_pair" "ssh" {
  count    = var.key_name != null ? 1 : 0
  key_name = var.key_name
}

# Without a key pair of your own, one is made from a public key you pass, and
# goes when the fleet does. The private half never leaves your machine, so it
# is not in the state either.
resource "aws_key_pair" "generated" {
  count      = var.key_name == null && var.ssh_public_key != null ? 1 : 0
  key_name   = "${var.name_prefix}-key"
  public_key = var.ssh_public_key

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-key" })
}

locals {
  key_name = (
    var.key_name != null ? data.aws_key_pair.ssh[0].key_name :
    length(aws_key_pair.generated) > 0 ? aws_key_pair.generated[0].key_name : null
  )
}

# Ubuntu, because PSI is compiled in and enabled by default. On Amazon Linux
# /proc/pressure/memory may be absent unless you boot with psi=1, and the
# collector silently reports 0.0 when the file is missing — a dashboard full of
# zeroes with nothing to explain it.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ---------------------------------------------------------------------------
# Security groups. Referencing each other by id rather than by CIDR means the
# addresses never have to be written down, and nothing is open wider than the
# one peer that needs it.
# ---------------------------------------------------------------------------

resource "aws_security_group" "server" {
  name        = "${var.name_prefix}-server"
  description = "SysHealth dashboard: metrics in from agents, UI for the operator"
  vpc_id      = data.aws_vpc.default.id

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-server" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "agent" {
  name        = "${var.name_prefix}-agent"
  description = "SysHealth agent: SSH for the operator, control port for the dashboard"
  vpc_id      = data.aws_vpc.default.id

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-agent" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "server_ssh" {
  security_group_id = aws_security_group.server.id
  description       = "SSH from the operator only"
  cidr_ipv4         = var.ssh_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

# /run-stress can load every agent. It needs the token too, but the UI is
# reachable from one address only all the same.
resource "aws_vpc_security_group_ingress_rule" "server_ui" {
  security_group_id = aws_security_group.server.id
  description       = "Dashboard UI, operator only"
  cidr_ipv4         = var.ssh_cidr
  from_port         = 5000
  to_port           = 5000
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "server_metrics" {
  security_group_id            = aws_security_group.server.id
  description                  = "Metrics pushed in by the agents"
  referenced_security_group_id = aws_security_group.agent.id
  from_port                    = 5000
  to_port                      = 5000
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "server_all" {
  security_group_id = aws_security_group.server.id
  description       = "Outbound: reach agent control ports, install packages"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "agent_ssh" {
  security_group_id = aws_security_group.agent.id
  description       = "SSH from the operator only"
  cidr_ipv4         = var.ssh_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "agent_control" {
  security_group_id            = aws_security_group.agent.id
  description                  = "Stress control port, reachable only from the dashboard"
  referenced_security_group_id = aws_security_group.server.id
  from_port                    = 5001
  to_port                      = 5001
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "agent_all" {
  security_group_id = aws_security_group.agent.id
  description       = "Outbound: push metrics, install packages"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------

resource "aws_instance" "server" {
  ami                    = local.ami_id
  instance_type          = var.server_instance_type
  subnet_id              = local.subnet_ids[0]
  vpc_security_group_ids = [aws_security_group.server.id]
  key_name               = local.key_name

  # Stated rather than inherited from the subnet's map_public_ip_on_launch.
  # Accounts do turn that off, and then there is no address to open the
  # dashboard on and no route out to install anything.
  associate_public_ip_address = true

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/server_user_data.sh.tftpl", {
    repo_url    = var.repo_url
    repo_branch = var.repo_branch
    token       = random_password.token.result
  })

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-dashboard"
    Role = "dashboard"
  })

  # A launch that cannot reach the EC2 API otherwise sits at "Still creating..."
  # indefinitely, printing progress that means nothing. Bound it, so a call that
  # is not landing says so instead of looking like a slow instance.
  timeouts {
    create = var.create_timeout
  }
}

resource "aws_instance" "agent" {
  for_each = local.instances

  ami           = local.ami_id
  instance_type = each.value.type

  # One zone per size, cycling if there are fewer subnets than sizes. Cross-zone
  # traffic costs a fraction of a cent per GB and these pushes are a few hundred
  # bytes every five seconds, so the capacity headroom is worth far more than the
  # transfer.
  subnet_id              = local.subnet_ids[each.value.index % length(local.subnet_ids)]
  vpc_security_group_ids = [aws_security_group.agent.id]
  key_name               = local.key_name

  # As above: the agents need a route out to clone the repo and install
  # packages, and an address you can SSH to when one of them misbehaves.
  associate_public_ip_address = true

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    repo_url    = var.repo_url
    repo_branch = var.repo_branch
    # The server's private address: same VPC, so metrics never leave it and
    # there is no public endpoint to write down or keep in sync.
    dashboard_url = "http://${aws_instance.server.private_ip}:5000/metrics"
    swap_mb       = var.swap_mb
    calibrate     = var.calibrate
    token         = random_password.token.result
  })

  # instance.py reads the instance type over IMDSv2, so require it rather than
  # leaving v1 open. No IAM role or credentials are involved: a machine can
  # always read its own metadata.
  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.agent_disk_gb
    encrypted   = true
  }

  tags = merge(local.common_tags, {
    Name         = "${var.name_prefix}-${replace(each.value.type, ".", "-")}"
    InstanceSize = each.value.type
    Role         = "agent"
  })

  timeouts {
    create = var.create_timeout
  }
}
