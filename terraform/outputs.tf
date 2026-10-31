output "dashboard_url" {
  description = "Open this. With domain_name, https://<it>, for anyone with a code; without, the server's own port, from ssh_cidr only."
  value       = local.https ? "https://${var.domain_name}" : "http://${aws_instance.server.public_ip}:5000"
}

output "direct_url" {
  description = "The server's own port, from ssh_cidr only: the way in that does not depend on DNS or the load balancer."
  value       = "http://${aws_instance.server.public_ip}:5000"
}

output "token" {
  description = "The server's token: the page needs it (or the watch-only code) to show anything, and it alone starts and stops tests. cli.py reads it from SYSHEALTH_TOKEN."
  value       = random_password.token.result
  sensitive   = true
}

output "view_token" {
  description = "The watch-only code: the page shows everything with it and changes nothing. The one to give an audience."
  value       = random_id.view_token.hex
  sensitive   = true
}

output "ami_id" {
  description = "The image these instances were built from. Read it before a destroy you intend to rebuild identically, and set it as ami_id in terraform.tfvars."
  value       = local.ami_id
}

output "instances" {
  description = "One entry per size, plus the dashboard."
  value = merge(
    {
      dashboard = {
        id         = aws_instance.server.id
        hostname   = aws_instance.server.private_dns
        public_ip  = aws_instance.server.public_ip
        private_ip = aws_instance.server.private_ip
        zone       = aws_instance.server.availability_zone
      }
    },
    {
      for size, inst in aws_instance.agent : size => {
        id         = inst.id
        hostname   = inst.private_dns
        public_ip  = inst.public_ip
        private_ip = inst.private_ip
        # Which zone a size landed in, so a capacity failure names a place.
        zone = inst.availability_zone
      }
    }
  )
}

output "ssh" {
  description = "Ready-to-paste SSH commands."
  value = merge(
    { dashboard = "ssh ubuntu@${aws_instance.server.public_ip}" },
    {
      for size, inst in aws_instance.agent :
      size => "ssh ubuntu@${inst.public_ip}"
    }
  )
}

output "logs" {
  description = "How to watch each box once it is up."
  value = merge(
    { dashboard = "ssh ubuntu@${aws_instance.server.public_ip} 'sudo journalctl -u syshealth-server -f'" },
    {
      for size, inst in aws_instance.agent :
      size => "ssh ubuntu@${inst.public_ip} 'sudo journalctl -u syshealth -f'"
    }
  )
}

output "next_steps" {
  description = "What to expect after apply."
  value       = <<-EOT
    Everything installs and starts itself. Nothing to run by hand.

    Open:  ${local.https ? "https://${var.domain_name}" : "http://${aws_instance.server.public_ip}:5000"}

    Allow two to three minutes: package install, ${var.calibrate ? "a 60s idle baseline calibration on each agent, " : ""}then the
    first pushes. The Instance toggle appears once two agents are reporting.

    The page asks for a code before it shows anything. Yours, which also
    starts and stops tests:      terraform output -raw token
    The watch-only one, to give an audience:  terraform output -raw view_token

    Open "Load test", enter the memory your app needs (peak, or a variable
    ramp), and watch the smaller sizes separate from the larger ones.

    If a size never appears:
      ssh ubuntu@<ip> 'sudo tail -50 /var/log/syshealth-bootstrap.log'
      ssh ubuntu@<ip> 'sudo journalctl -u syshealth -n 50'

    History lives in the server's memory, capped at one hour per instance, and
    is lost if the server restarts. Fine for a few days of spot-checking; not a
    record you can go back through.

    These five instances${local.https ? " and the load balancer" : ""} bill until you stop them:
      terraform destroy
  EOT
}
