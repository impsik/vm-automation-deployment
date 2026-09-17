packer {
  required_plugins {
    qemu = {
      version = "= 1.1.3"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "output_directory" {
  type    = string
  default = "/home/imre/chia/Hetznerist/kvm/templates/ubuntu-22.04-lvm"
}

source "qemu" "ubuntu_lvm" {
  accelerator = "kvm"
  boot_wait   = "5s"
  boot_command = [
    "<esc><wait>",
    "e<wait>",
    "<down><down><down><end>",
    " autoinstall ds=nocloud-net\\;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/ ---",
    "<f10>"
  ]
  cpus             = 2
  disk_compression = true
  disk_image       = false
  disk_interface   = "virtio"
  disk_size        = "20G"
  format           = "qcow2"
  headless         = true
  http_directory   = "http"
  iso_checksum     = "file:https://releases.ubuntu.com/22.04/SHA256SUMS"
  iso_url          = "https://releases.ubuntu.com/22.04/ubuntu-22.04.5-live-server-amd64.iso"
  memory           = 4096
  net_device       = "virtio-net"
  output_directory = var.output_directory
  shutdown_command = "echo packer | sudo -S sh -c 'passwd -l packer; rm -f /etc/sudoers.d/packer; shutdown -P now'"
  ssh_password     = "packer"
  ssh_timeout      = "35m"
  ssh_username     = "packer"
  vm_name          = "ubuntu-22.04-lvm.qcow2"
}

build {
  sources = ["source.qemu.ubuntu_lvm"]

  provisioner "shell" {
    execute_command = "echo packer | sudo -S -E sh '{{ .Path }}'"
    inline = [
      "sudo systemctl enable qemu-guest-agent",
      "sudo cloud-init clean --logs --machine-id",
      "sudo rm -f /etc/ssh/ssh_host_*",
      "sudo apt-get clean",
      "sync"
    ]
  }
}
