import { check } from "k6";
import http from "k6/http";

const orderVUs = Number(__ENV.ORDER_VUS || 250);
if (!Number.isInteger(orderVUs) || orderVUs < 1) {
  throw new Error("ORDER_VUS must be a positive integer");
}

export const options = {
  scenarios: {
    orders: {
      executor: "constant-arrival-rate",
      rate: 100,
      timeUnit: "1s",
      duration: "5m",
      preAllocatedVUs: orderVUs,
      maxVUs: orderVUs,
    },
  },
};

export default function () {
  const response = http.post(
    `http://localhost:8081/api/order`,
    JSON.stringify({
      orderLineItemsDtoList: [
        {
          skuCode: "iphone_13",
          price: 200,
          quantity: 1,
        },
      ],
    }),
    {
      headers: {
        "Content-Type": "application/json",
      },
    },
  );

  check(response, {
    "order accepted": (r) =>
      r.status === 201 && r.body.includes("Placed Successfully"),
  });
}
