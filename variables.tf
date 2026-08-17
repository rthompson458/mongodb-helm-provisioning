variable "mongodb_databases" {
  description = "MongoDB databases and collections to provision"

  type = list(object({
    name        = string
    collections = list(string)
  }))

  default = [
    {
      name = "Cars"
      collections = [
        "make",
        "model",
        "year",
        "color"
      ]
    },
    {
      name = "Inventory"
      collections = [
        "products",
        "warehouses"
      ]
    }
  ]
}

variable "mongodb_request_users" {
  description = "Request-driven MongoDB users to provision"

  type = list(object({
    resourceName           = string
    username               = string
    authDatabase           = string
    passwordSecret         = string
    passwordKey            = string
    connectionStringSecret = string

    roles = list(object({
      db   = string
      name = string
    }))
  }))

  default = []
}

variable "mongodb_database_operations" {
  description = "One-shot controlled MongoDB database and collection management operations"

  type = list(object({
    id         = string
    action     = string
    database   = string
    collection = string
    newName    = string
  }))

  default = []
}
