//! Cache belongs to the checked-out physical connection, never to the provider.
use deadpool_postgres::ClientWrapper;
use tokio_postgres::{error::SqlState, Error, Statement};

// Dynamic SQL must not grow each connection's cache without bound. Clearing at
// capacity is deliberately simple; hot fixed-shape queries stay cached below it.
pub const MAX_CACHED_STATEMENTS: usize = 128;

pub async fn prepare(client: &ClientWrapper, query: &str) -> Result<Statement, Error> {
    if client.statement_cache.size() >= MAX_CACHED_STATEMENTS {
        client.statement_cache.clear();
    }
    client.prepare_cached(query).await
}

pub fn invalidate_on_error(client: &ClientWrapper, error: &Error) {
    // DEALLOCATE/DISCARD or a schema change can invalidate a prepared statement.
    // Do not replay the failed query: a write must execute at most once per call.
    if let Some(code) = error.code() {
        if code == &SqlState::INVALID_SQL_STATEMENT_NAME || code == &SqlState::FEATURE_NOT_SUPPORTED {
            client.statement_cache.clear();
        }
    }
}
