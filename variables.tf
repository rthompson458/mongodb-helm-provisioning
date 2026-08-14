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
