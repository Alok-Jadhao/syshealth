# Your own address for the dashboard, made once and kept: a fixed IP address
# for a name such as syshealth.example.com, and an HTTPS certificate for it.
# The fleet (../main.tf, with domain_name set) puts its load balancer on that
# address each time it is built, and takes it away again on destroy; these
# stay, so the two records you gave your DNS host keep working.
#
# Two plain records, an A and a CNAME, and no zone of its own: many DNS hosts
# (Hostinger among them) cannot hand a subdomain to other name servers.
#
# ../../aws.sh domain NAME runs this and says what to add at your DNS host. By
# hand:
#
#   terraform -chdir=terraform/domain init
#   terraform -chdir=terraform/domain apply -var domain_name=syshealth.example.com
#
# The address costs $3.65 a month, held or in use ($0.005 an hour, as every
# public IPv4 address on AWS); the certificate is free and renews itself.

terraform {
  required_version = ">= 1.3"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "domain_name" {
  description = "The name the dashboard is to answer on, a subdomain of a domain you own, e.g. syshealth.example.com."
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.){2,}[a-z]{2,}$", var.domain_name))
    error_message = "domain_name must be a subdomain in lower case, like syshealth.example.com: a name of its own, so the rest of your domain stays where it is."
  }
}

variable "region" {
  description = "Where the address and the certificate are made. A load balancer can only use them in its own region, so this must be the fleet's."
  type        = string
  default     = "ap-south-1"
}

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}
}

provider "aws" {
  region = var.region
}

locals {
  tags = merge(var.tags, {
    Project   = "syshealth"
    ManagedBy = "terraform"
  })

  # Names at a DNS host are written without the domain they are in.
  labels = split(".", var.domain_name)
  parent = join(".", slice(local.labels, 1, length(local.labels)))
}

# The fleet finds it by the SysHealthAddress tag.
resource "aws_eip" "dashboard" {
  domain = "vpc"
  tags = merge(local.tags, {
    Name             = "syshealth-dashboard"
    SysHealthAddress = var.domain_name
  })
}

resource "aws_acm_certificate" "this" {
  domain_name       = var.domain_name
  validation_method = "DNS"
  tags              = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

# ACM proves the name is yours by looking up the CNAME below, at your DNS
# host: until it is there, this waits. It must stay there afterwards, too, for
# the certificate to renew itself.
resource "aws_acm_certificate_validation" "this" {
  certificate_arn = aws_acm_certificate.this.arn

  timeouts {
    create = "45m"
  }
}

output "address" {
  description = "The fixed IP address the dashboard's load balancer gets on every up."
  value       = aws_eip.dashboard.public_ip
}

output "records" {
  description = "What to add where the parent domain's DNS is kept: name relative to that domain, and the full name."
  value = concat(
    [{
      type  = "A"
      name  = local.labels[0]
      fqdn  = var.domain_name
      value = aws_eip.dashboard.public_ip
    }],
    [for o in aws_acm_certificate.this.domain_validation_options : {
      type  = o.resource_record_type
      name  = trimsuffix(trimsuffix(o.resource_record_name, "."), ".${local.parent}")
      fqdn  = trimsuffix(o.resource_record_name, ".")
      value = trimsuffix(o.resource_record_value, ".")
    }]
  )
}

output "certificate_arn" {
  description = "The certificate the fleet's load balancer serves."
  value       = aws_acm_certificate_validation.this.certificate_arn
}
