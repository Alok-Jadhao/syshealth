variable "region" {
  description = <<-EOT
    AWS region to launch into. Note that key pairs are per region — the name in
    key_name must exist in this one. Larger regions run short of a given instance
    type less often; ap-south-1, eu-west-1 and eu-central-1 are all considerably
    bigger than eu-north-1.
  EOT
  type        = string
  default     = "ap-south-1"
}

variable "instance_types" {
  description = "The sizes to compare. Each becomes one instance running the same agent."
  type        = list(string)
  default     = ["t3.micro", "t3.small", "t3.medium", "t3.large"]
}

variable "server_instance_type" {
  description = "Size of the dashboard server. It only collects and serves JSON, so it does not need much."
  type        = string
  default     = "t3.small"
}

variable "key_name" {
  description = "Name of an existing EC2 key pair in this region, used for SSH."
  type        = string
}

variable "ssh_cidr" {
  description = "Who may SSH in and open the dashboard, as an IPv4 CIDR (e.g. \"203.0.113.4/32\"). Deliberately has no default — do not open this to the world by accident."
  type        = string

  validation {
    condition     = var.ssh_cidr != "0.0.0.0/0"
    error_message = "Refusing 0.0.0.0/0. The dashboard exposes /run-stress with no authentication, so anyone reaching it could load your instances. Use your own address, e.g. \"203.0.113.4/32\"."
  }

  # Caught here because the provider's own complaint is three identical repeats
  # of "must be a valid IPv4 CIDR", one per rule, with no hint as to why.
  validation {
    condition     = can(cidrnetmask(var.ssh_cidr))
    error_message = <<-EOT
      ssh_cidr must be an IPv4 CIDR, like "203.0.113.4/32".

      If you built it from `curl ifconfig.me` and got an address full of colons,
      that was your IPv6 address — many ISPs hand out IPv6 by default. Ask for
      IPv4 explicitly:

          curl -4 -s ifconfig.me

      An IPv6 rule would not help here anyway: instances in the default VPC get
      IPv4 addresses, so that is the family the rules have to match.
    EOT
  }

  # A /24 must be named as x.x.x.0/24, not by some host inside it. Skipped when
  # the value is not IPv4 at all, so that failure reports once, above.
  validation {
    condition = (
      !can(cidrnetmask(var.ssh_cidr)) ||
      cidrhost(var.ssh_cidr, 0) == split("/", var.ssh_cidr)[0]
    )
    error_message = "ssh_cidr must name the start of its block. For a single address use a /32, e.g. \"203.0.113.4/32\"; for a range, name the network address, e.g. \"203.0.113.0/24\"."
  }
}

variable "repo_url" {
  description = "Git repository the instances install from."
  type        = string
  default     = "https://github.com/aryangorde8/syshealth.git"
}

variable "repo_branch" {
  description = "Branch to check out on each instance. Point this at main once the dashboard is merged there; until then the default is the branch carrying it."
  type        = string
  default     = "feat/pressure-graph"
}

variable "swap_mb" {
  description = <<-EOT
    Swapfile size in MiB on the agents, or 0 for none. Worth keeping: with no
    swap, a stress run on a small instance hits the OOM killer almost
    immediately, so you see a spike and a dead process instead of sustained
    reclaim. Swap gives the kernel something to reclaim, which is what PSI
    actually measures.
  EOT
  type        = number
  default     = 512
}

variable "calibrate" {
  description = "Run the 60s idle baseline calibration at first boot. The baseline is reported as each machine's idle reference; health states use fixed thresholds (docs/thresholds.md) and do not depend on it."
  type        = bool
  default     = true
}

variable "ami_id" {
  description = <<-EOT
    Pin the machine image instead of taking Canonical's newest. Leave null for
    day-to-day use.

    Set it when you intend to destroy the fleet and rebuild it later expecting
    the same result — before a demo, say. The AMI lookup asks for most_recent,
    which means "whatever Canonical published this morning": a fleet you
    rehearsed on and a fleet you relaunch a week later are not built from the
    same image, and the one difference you cannot debug under time pressure is
    the one you did not know you had.

    Read the current value out before you destroy:
      terraform output ami_id
  EOT
  type        = string
  default     = null
}

variable "subnet_id" {
  description = <<-EOT
    Pin every instance to one subnet. Leave null to spread across the default
    VPC's subnets, which is what you want: EC2 capacity is per availability zone
    per instance type, so one zone short of t3 should not stop the whole fleet.
    Set this only when you have found a zone that does have capacity and want
    everything there.
  EOT
  type        = string
  default     = null
}

variable "create_timeout" {
  description = <<-EOT
    How long to wait for an instance to reach "running" before failing. An
    instance normally gets there in well under a minute; anything approaching
    this bound means the EC2 API call is not getting through, not that AWS is
    being slow. Raise it if you are on a connection you know to be slow but
    working.
  EOT
  type        = string
  default     = "8m"
}

variable "name_prefix" {
  description = "Prefix for instance and security group names."
  type        = string
  default     = "syshealth"
}

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}
}
