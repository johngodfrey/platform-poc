#[path = "../cached_statement.rs"]
mod cached_statement;
use deadpool_postgres::{Manager, Pool};
use tokio_postgres::{error::SqlState, NoTls};

fn pool(size: usize) -> Pool {
    let config = std::env::var("CACHE_TEST_PG_CONFIG")
        .expect("Set CACHE_TEST_PG_CONFIG to a local disposable-test connection")
        .parse().unwrap();
    Pool::builder(Manager::new(config, NoTls)).max_size(size).build().unwrap()
}

#[tokio::test]
async fn reuse_is_per_physical_connection_and_survives_pool_checkout() {
    let pool = pool(2);
    let a = pool.get().await.unwrap();
    let b = pool.get().await.unwrap();
    let sql = "SELECT $1::int4 AS value";
    for value in [7_i32, 19] {
        let stmt = cached_statement::prepare(&a, sql).await.unwrap();
        assert_eq!(a.query_one(&stmt, &[&value]).await.unwrap().get::<_, i32>(0), value);
    }
    assert_eq!(a.statement_cache.size(), 1);
    assert_eq!(b.statement_cache.size(), 0);
    let statement = cached_statement::prepare(&b, sql).await.unwrap();
    assert_eq!(b.query_one(&statement, &[&23_i32]).await.unwrap().get::<_, i32>(0), 23);
    assert_eq!(b.statement_cache.size(), 1);
    let count: i64 = a.query_one("SELECT count(*) FROM pg_prepared_statements WHERE statement=$1", &[&sql]).await.unwrap().get(0);
    assert_eq!(count, 1);
    // A separate one-slot pool makes same-connection checkout deterministic.
    let pool = self::pool(1);
    let a = pool.get().await.unwrap();
    cached_statement::prepare(&a, sql).await.unwrap();
    drop(a);
    let a = pool.get().await.unwrap();
    assert_eq!(a.statement_cache.size(), 1);
    let stmt = cached_statement::prepare(&a, sql).await.unwrap();
    assert_eq!(a.query_one(&stmt, &[&31_i32]).await.unwrap().get::<_, i32>(0), 31);
}

#[tokio::test]
async fn dynamic_sql_is_bounded_and_bad_sql_is_not_cached() {
    let pool = pool(1); let client = pool.get().await.unwrap();
    for n in 0..(cached_statement::MAX_CACHED_STATEMENTS * 2 + 5) {
        let stmt = cached_statement::prepare(&client, &format!("SELECT {n}::int4")).await.unwrap();
        assert_eq!(client.query_one(&stmt, &[]).await.unwrap().get::<_, i32>(0), n as i32);
        assert!(client.statement_cache.size() <= cached_statement::MAX_CACHED_STATEMENTS);
    }
    let size = client.statement_cache.size();
    assert!(cached_statement::prepare(&client, "NOT VALID SQL").await.is_err());
    assert_eq!(client.statement_cache.size(), size);
}

#[tokio::test]
async fn invalid_statement_is_reported_then_cleared_without_replay() {
    let pool = pool(1);let client = pool.get().await.unwrap();
    let sql = "SELECT $1::int4";
    let stmt = cached_statement::prepare(&client, sql).await.unwrap();
    client.batch_execute("DEALLOCATE ALL").await.unwrap();
    let error = client.query_one(&stmt, &[&8_i32]).await.unwrap_err();
    assert_eq!(error.code(), Some(&SqlState::INVALID_SQL_STATEMENT_NAME));
    cached_statement::invalidate_on_error(&client, &error);
    assert_eq!(client.statement_cache.size(), 0);
    let fresh = cached_statement::prepare(&client, sql).await.unwrap();
    assert_eq!(client.query_one(&fresh, &[&9_i32]).await.unwrap().get::<_, i32>(0), 9);
}

#[tokio::test]
async fn constraint_failure_does_not_repeat_writes() {
    let pool = pool(1);let client = pool.get().await.unwrap();
    client.batch_execute("CREATE TEMP TABLE cache_test_write (id int PRIMARY KEY)").await.unwrap();
    let stmt = cached_statement::prepare(&client, "INSERT INTO cache_test_write VALUES ($1)").await.unwrap();
    client.execute(&stmt, &[&1_i32]).await.unwrap();
    let error = client.execute(&stmt, &[&1_i32]).await.unwrap_err();
    cached_statement::invalidate_on_error(&client, &error);
    assert_eq!(error.code(), Some(&SqlState::UNIQUE_VIOLATION));
    assert_eq!(client.query_one("SELECT count(*) FROM cache_test_write", &[]).await.unwrap().get::<_, i64>(0), 1);
    assert_eq!(client.statement_cache.size(), 1);
}

#[tokio::test]
async fn changed_result_schema_clears_cache_for_the_next_call() {
    let pool = pool(1);let client = pool.get().await.unwrap();
    client.batch_execute("CREATE TEMP TABLE cache_test_schema (value int); INSERT INTO cache_test_schema VALUES (1)").await.unwrap();
    let query = "SELECT * FROM cache_test_schema";
    let stmt = cached_statement::prepare(&client, query).await.unwrap();
    assert_eq!(client.query_one(&stmt, &[]).await.unwrap().get::<_, i32>(0), 1);
    client.batch_execute("ALTER TABLE cache_test_schema ALTER COLUMN value TYPE bigint").await.unwrap();
    let error = client.query_one(&stmt, &[]).await.unwrap_err();
    assert_eq!(error.code(), Some(&SqlState::FEATURE_NOT_SUPPORTED));
    cached_statement::invalidate_on_error(&client, &error);
    assert_eq!(client.statement_cache.size(), 0);
    let fresh = cached_statement::prepare(&client, query).await.unwrap();
    assert_eq!(client.query_one(&fresh, &[]).await.unwrap().get::<_, i64>(0), 1);
}

#[tokio::test]
async fn reconnect_creates_a_fresh_cache() {
    let pool = pool(1);let client = pool.get().await.unwrap();
    cached_statement::prepare(&client, "SELECT 1").await.unwrap();
    let pid: i32 = client.query_one("SELECT pg_backend_pid()", &[]).await.unwrap().get(0);
    let _ = client.query("SELECT pg_terminate_backend(pg_backend_pid())", &[]).await;
    for _ in 0..100 {
        if client.is_closed() { break; }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    assert!(client.is_closed());drop(client);
    let fresh = pool.get().await.unwrap();
    assert_eq!(fresh.statement_cache.size(), 0);
    let new_pid: i32 = fresh.query_one("SELECT pg_backend_pid()", &[]).await.unwrap().get(0);
    assert_ne!(pid, new_pid);
    let stmt = cached_statement::prepare(&fresh, "SELECT 1::int4").await.unwrap();
    assert_eq!(fresh.query_one(&stmt, &[]).await.unwrap().get::<_, i32>(0), 1);
}
