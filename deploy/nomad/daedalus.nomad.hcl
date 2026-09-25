# Daedalus on Nomad: one group, two tasks from the same image.
#
# Assumptions - adjust to your cluster:
#   * the image is in a registry your clients can pull from (build it with
#     `docker build -t registry.example.com/daedalus:1.0.0 .`)
#   * PostgreSQL runs elsewhere; the DSN and SNMP communities come from Nomad
#     Variables at nomad/jobs/daedalus (or replace the template with Vault)
#   * Traefik (or another proxy) routes daedalus.example.com to the web task and
#     puts authentication in front of it - the UI has no login of its own
#   * the collector needs to reach switches and firewall via SNMP (UDP 161);
#     host networking keeps that simple and lets Wake-on-LAN broadcasts work
#
# Create the variables once:
#   nomad var put nomad/jobs/daedalus dsn='postgresql://daedalus:...@db:5432/daedalus' \
#       snmp_firewall='...' snmp_switch='...'

job "daedalus" {
  type = "service"

  group "daedalus" {
    count = 1   # exactly one collector - two would collect everything twice

    network {
      mode = "host"
      port "http" { static = 8000 }
    }

    service {
      name     = "daedalus"
      port     = "http"
      provider = "nomad"
      tags = [
        "traefik.enable=true",
        "traefik.http.routers.daedalus.rule=Host(`daedalus.example.com`)",
        "traefik.http.routers.daedalus.entrypoints=websecure",
        "traefik.http.routers.daedalus.tls=true",
        # "traefik.http.routers.daedalus.middlewares=auth@file",
      ]
      check {
        type     = "http"
        path     = "/ready"
        interval = "30s"
        timeout  = "5s"
      }
    }

    task "collector" {
      driver = "docker"
      config {
        image        = "registry.example.com/daedalus:1.0.0"
        network_mode = "host"
        command      = "python"
        args         = ["run.py", "--service"]
      }
      template {
        destination = "secrets/daedalus.env"
        env         = true
        data        = <<-EOT
          {{ with nomadVar "nomad/jobs/daedalus" }}
          DAEDALUS_DSN={{ .dsn }}
          DAEDALUS_SNMP_FIREWALL={{ .snmp_firewall }}
          DAEDALUS_SNMP_SWITCH={{ .snmp_switch }}
          {{ end }}
          DAEDALUS_CONFIG=/alloc/daedalus.toml
        EOT
      }
      # The site configuration, written into the shared alloc directory so the web
      # task reads the same file. Paste your daedalus.toml here, or fetch it with
      # an `artifact` block from a repository.
      template {
        destination = "${NOMAD_ALLOC_DIR}/daedalus.toml"
        data        = <<-EOT
          timezone    = "UTC"
          root_switch = "bb8"

          [firewall]
          address = "172.16.1.1"

          [switches.bb8]
          address = "172.16.0.4"
          model   = "SG200-26"
          ports   = 26

          [[networks]]
          cidr = "172.16.10.0/24"
          vlan = 10
          name = "INTERNAL"
        EOT
      }
      resources {
        cpu    = 200
        memory = 192
      }
    }

    task "web" {
      driver = "docker"
      config {
        image        = "registry.example.com/daedalus:1.0.0"
        network_mode = "host"
        command      = "uvicorn"
        args         = ["daedalus.web:app", "--host", "0.0.0.0", "--port", "${NOMAD_PORT_http}",
                        "--proxy-headers", "--forwarded-allow-ips", "*"]
      }
      template {
        destination = "secrets/daedalus.env"
        env         = true
        data        = <<-EOT
          {{ with nomadVar "nomad/jobs/daedalus" }}
          DAEDALUS_DSN={{ .dsn }}
          {{ end }}
          DAEDALUS_CONFIG=/alloc/daedalus.toml
          DAEDALUS_SCAN_ENABLED=no
        EOT
      }
      resources {
        cpu    = 200
        memory = 256
      }
    }
  }
}
