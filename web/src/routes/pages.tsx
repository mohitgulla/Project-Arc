import { Link } from "react-router-dom";

import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";

export const NotFoundPage = () => (
  <Card title="Not found">
    <EmptyState caption="No page here.">
      <Link to="/" className="arc-action">
        Overview ↗
      </Link>
    </EmptyState>
  </Card>
);
